"""Chain-FCC harness — the chain analogue of ``fcc_eval`` (§6, §12 Phase 1).

Where ``fcc_eval`` scores ONE tool pick, this proves a whole ``graph.plan()`` is
*first-plan-correct*: run each ``PlanStep`` through the existing recorded-mode
``prepare``/``call`` path, thread step N's synthesized output field (named by the
``feeds`` edge's ``source_field``) into step N+1's consuming param, and score the
chain **well-formed** (the shared caller guard) AND **value-kind-correct** (each
threaded value's ``value_kind`` matches the consuming param's declared type).

Recorded mode only: $0, no live calls, no new corpus — a whole plan is falsifiable
offline (§6). Control-plane clean: records booleans, value-KINDS, and field names,
never a raw threaded value or a response payload.

Scope (§12 Phase 1): single-API, clean-name specs. Threading uses the real API
param names the plan carries; a spec whose agent-facing keys were sanitized away
from the real name (JSON:API ``filter[user]``) is out of this phase's scope and
would surface as a not-well-formed step (an honest failure, not a silent pass).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .caller import CallError
from .client import AgentApiClient
from .fcc_eval import ValueKind, value_kind
from .graph import Plan
from .ingest import Operation
from .tools import tool_name

# A threaded value is a join key: it must land in the consuming param's declared
# type. ``value_kind`` classifies the VALUE; this maps the PARAM's JSON-Schema type
# to the kinds that satisfy it. A string param accepts any id-shaped string (a
# numeric string reads as ``int`` via ``value_kind``); integer/number/boolean are exact.
_TYPE_KINDS: dict[str, frozenset[ValueKind]] = {
    "integer": frozenset({"int"}),
    "number": frozenset({"int", "float"}),
    "string": frozenset({"symbol", "mint", "int"}),
    "boolean": frozenset({"bool"}),
}


def kind_matches_type(kind: ValueKind, param_type: str | None) -> bool:
    """True iff a threaded value of ``kind`` is shape-valid for a param of
    ``param_type``. Unknown/untyped param -> False: we cannot confirm the thread is
    correct, so it is not counted as first-plan-correct (honest, not optimistic)."""
    if param_type is None:
        return False
    allowed = _TYPE_KINDS.get(param_type)
    return allowed is not None and kind in allowed


@dataclass(frozen=True)
class ThreadOutcome:
    """One value threaded across a ``feeds`` edge — control-plane clean (names + KIND)."""

    param: str  # the consuming param the value was threaded into
    source_op: str
    source_field: str
    found: bool  # the source_field existed in the producer's synthesized response
    value_kind: ValueKind  # kind of the threaded value ("none" when not found)
    param_type: str | None  # the consuming param's declared JSON-Schema type
    kind_ok: bool  # value_kind matches the consuming param type


@dataclass(frozen=True)
class ChainStepResult:
    operation_id: str
    well_formed: bool
    reason: str = ""  # why a step failed (a caught CallError message), else ""


@dataclass(frozen=True)
class ChainFccResult:
    """The chain analogue of ``FccScore``: per-step + threads + overall + why."""

    steps: tuple[ChainStepResult, ...]
    threads: tuple[ThreadOutcome, ...]
    first_plan_correct: bool
    reason: str

    @property
    def all_well_formed(self) -> bool:
        return all(s.well_formed for s in self.steps)


def _find_field(data: Any, name: str) -> tuple[bool, Any]:
    """DFS the synthesized response for the first key == ``name``; deterministic
    because ``example_from_schema`` is deterministic. Returns (found, value)."""
    if isinstance(data, Mapping):
        if name in data:
            return True, data[name]
        for v in data.values():
            found, val = _find_field(v, name)
            if found:
                return found, val
    elif isinstance(data, (list, tuple)):
        for item in data:
            found, val = _find_field(item, name)
            if found:
                return found, val
    return False, None


def _param_type(op: Operation, name: str) -> str | None:
    for p in op.parameters:
        if p.name == name:
            sch = p.schema if isinstance(p.schema, dict) else {}
            t = sch.get("type")
            return t if isinstance(t, str) else None
    return None


@dataclass(frozen=True)
class _Threading:
    """Bookkeeping for one supplier->consumer thread declared by an explain entry."""

    param: str
    source_op: str
    source_field: str
    consumer_type: str | None


def evaluate_chain(
    client: AgentApiClient,
    plan: Plan,
    seed_args: Mapping[str, Any] | None = None,
) -> ChainFccResult:
    """Execute ``plan`` in recorded mode, threading produced fields into later
    steps, and score the whole chain first-plan-correct.

    ``seed_args`` are the values the agent's intent already supplies (mapped by real
    param name) — applied to any step that consumes them. Threaded values come from
    the plan's ``feeds`` edges and are never seeded. The chain is first-plan-correct
    iff every step is well-formed AND every threaded value was found and is
    value-kind-correct for its consuming param.
    """
    seed = dict(seed_args or {})
    op_by_opid = {op.operation_id: op for op in client.operations}

    # Resolve each explain entry to its consumer op's declared param type once. The
    # consumer is the plan step (other than the source) that consumes this param.
    threadings: dict[str, _Threading] = {}
    for e in plan.explain:
        consumer_type: str | None = None
        for step in plan.steps:
            if step.operation_id == e.source_op or e.param not in step.consumes:
                continue
            consumer_op = op_by_opid.get(step.operation_id)
            if consumer_op is not None:
                consumer_type = _param_type(consumer_op, e.param)
            break
        threadings[e.param] = _Threading(
            param=e.param,
            source_op=e.source_op,
            source_field=e.source_field,
            consumer_type=consumer_type,
        )

    context: dict[str, Any] = dict(seed)
    step_results: list[ChainStepResult] = []
    thread_outcomes: list[ThreadOutcome] = []
    supplies_by_op: dict[str, list[str]] = {}
    for step in plan.steps:
        supplies_by_op.setdefault(step.operation_id, []).extend(step.supplies)

    for step in plan.steps:
        opid = step.operation_id
        op = op_by_opid.get(opid)
        if op is None:
            step_results.append(ChainStepResult(opid, False, "unknown operation"))
            continue
        tname = tool_name(op)
        # Every required input this step consumes, drawn from seed + prior threads. A
        # missing one is left absent so the caller guard catches it (not well-formed).
        args = {name: context[name] for name in step.consumes if name in context}
        try:
            result = client.call(tname, args, mode="recorded")
            step_results.append(ChainStepResult(opid, True))
        except CallError as exc:
            step_results.append(ChainStepResult(opid, False, str(exc)))
            continue

        # Thread this step's supplied fields into the context for later steps.
        for supplied in supplies_by_op.get(opid, []):
            th = threadings.get(supplied)
            if th is None or th.source_op != opid:
                continue
            found, value = _find_field(result.get("data"), th.source_field)
            context[supplied] = value
            vk = value_kind(value)
            thread_outcomes.append(
                ThreadOutcome(
                    param=th.param,
                    source_op=th.source_op,
                    source_field=th.source_field,
                    found=found,
                    value_kind=vk,
                    param_type=th.consumer_type,
                    kind_ok=found and kind_matches_type(vk, th.consumer_type),
                )
            )

    all_well_formed = all(s.well_formed for s in step_results)
    threads_ok = all(t.kind_ok for t in thread_outcomes)
    first_plan_correct = all_well_formed and threads_ok
    reason = _reason(step_results, thread_outcomes, all_well_formed, threads_ok)
    return ChainFccResult(
        steps=tuple(step_results),
        threads=tuple(thread_outcomes),
        first_plan_correct=first_plan_correct,
        reason=reason,
    )


def _reason(
    steps: list[ChainStepResult],
    threads: list[ThreadOutcome],
    all_well_formed: bool,
    threads_ok: bool,
) -> str:
    if all_well_formed and threads_ok:
        return "first-plan-correct: every step well-formed, every thread kind-correct"
    parts: list[str] = []
    for s in steps:
        if not s.well_formed:
            parts.append(f"step {s.operation_id} not well-formed: {s.reason}")
    for t in threads:
        if not t.found:
            parts.append(
                f"thread {t.param}: source_field '{t.source_field}' not in "
                f"{t.source_op}'s synthesized response"
            )
        elif not t.kind_ok:
            parts.append(
                f"thread {t.param}: value_kind '{t.value_kind}' does not match "
                f"consuming param type '{t.param_type}'"
            )
    return "; ".join(parts) or "chain failed"
