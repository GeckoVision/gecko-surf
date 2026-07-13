"""Agent-in-the-loop self-heal eval — does the offline sandbox TEACH a correlated
multi-step workflow? (self-healing-loop plan, Task 5.2)

Where ``fcc_eval`` measures a SINGLE first-call pick and ``flywheel_eval`` measures the
CORPUS lift, this measures the **self-heal loop**: a correlated escrow flow the agent must
discover — ``deposit(amount)`` must run BEFORE ``withdraw(amount)`` or the second call comes
back a synthetic 422 "insufficient" from the per-session :class:`~gecko.sandbox.SimWorld`.

The precondition is deliberately **undocumented** — neither the goal nor the tool
descriptions state "fund the account first" (an early leaky version let Haiku one-shot the
ordering, pinning the baseline at 100% and leaving the loop nothing to prove). The dependency
is a HIDDEN semantic rule the SimWorld enforces; the agent can learn it only from the failure
— and, in the self-heal arm, from the remediation + ``query_docs``. That is the actual test:
does the loop teach a precondition the docs never mention?

Two arms, same neutral system prompt, same callable tools (deposit + withdraw), same goal:
  - **BASELINE** — no ``query_docs`` tool; a failed probe call returns the API's own error
    BODY only (no ``signals``/``remediation``). The agent is not told WHY it failed.
  - **SELF-HEAL** — ``query_docs`` is presented AND a failed call returns the full sandbox
    result (``signals`` + ``remediation`` + the synthetic-mode note). The self-heal input.

The question the numbers answer: does SELF-HEAL raise multi-step completion over BASELINE?
A thin or zero lift is a REAL finding (Pattern B) — this module never fabricates a pass.

Everything is probe mode: ``sandbox.evaluate`` is the transport edge, so no wire, no auth,
no corpus write (invariant #1/#3). The ``SimWorld`` is per-episode and process-local; nothing
accumulates across arms or runs. The LLM is an injected seam (Anthropic messages shape), so
the loop is offline-mockable — tests drive it with a scripted fake; only the runner talks to
Haiku, and even then $0 against the real API (probe is synthetic).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from typing import Any, Protocol

from .client import AgentApiClient
from .docsearch import search_docs
from .ingest import Operation, extract_operations
from .sandbox import SimStore, evaluate

# --- the correlated escrow surface (synthetic; the SimWorld supplies the state) ---------
#
# Two value-moving ops whose sim-rules the sandbox AUTO-derives from their verb + amount
# shape (invariant #2): "deposit" credits the session balance, "withdraw" debits it and
# 422s when the balance is short. No per-API sandbox code — the correlation is emergent.

ESCROW_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Escrow Settlement API", "version": "1"},
    "servers": [{"url": "https://escrow.example.com"}],
    "paths": {
        "/deposit": {
            "post": {
                "operationId": "createDeposit",
                # Neutral, NON-leaky: describes WHAT the op does, never that it is a
                # precondition of a withdrawal. The ordering dependency is a HIDDEN semantic
                # rule the SimWorld enforces but the docs do not state — so the agent must
                # DISCOVER it (the whole point of the self-heal test). Keeps "credit"/"fund"
                # so the remediation string ("credit the account first") can lexically link
                # a query_docs search back to this op.
                "summary": "Add funds to (credit) the escrow account.",
                "description": "Credit the escrow account balance by the given amount.",
                "parameters": [
                    {
                        "name": "amount",
                        "in": "query",
                        "required": True,
                        "description": "Amount to credit, in whole units.",
                        "schema": {"type": "number"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    }
                },
            }
        },
        "/withdraw": {
            "post": {
                "operationId": "createWithdraw",
                # Neutral, NON-leaky: no mention that a deposit must precede it. The 422
                # insufficient-balance precondition is undocumented here — the agent learns
                # it ONLY from the synthetic failure (+ remediation, in the self-heal arm).
                "summary": "Release funds from the escrow account to the counterparty.",
                "description": "Debit the escrow account and release the given amount.",
                "parameters": [
                    {
                        "name": "amount",
                        "in": "query",
                        "required": True,
                        "description": "Amount to release, in whole units.",
                        "schema": {"type": "number"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    },
                    "422": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error_code": {"type": "string"},
                                        "detail": {"type": "string"},
                                    },
                                    "required": ["error_code"],
                                }
                            }
                        }
                    },
                },
            }
        },
    },
}

DEPOSIT_OP = "createDeposit"
WITHDRAW_OP = "createWithdraw"


@dataclass(frozen=True)
class MultiStepTask:
    """One correlated flow: ``settle_op`` (withdraw) 200s only after ``prereq_op`` (deposit)
    funded the session balance. ``amount`` is what the goal asks to release; the balance
    starts at zero, so a naive single call to ``settle_op`` is guaranteed to 422."""

    goal: str
    prereq_op: str
    settle_op: str
    amount: float


def escrow_tasks() -> list[MultiStepTask]:
    """The small multi-step golden — correlated escrow settlements the agent must discover."""
    # The goals state ONLY the intent — never that the account must be funded first. The
    # deposit-before-withdraw precondition is undocumented in both the goal and the tool
    # descriptions, so a correct completion has to DISCOVER it from the failure.
    return [
        MultiStepTask(
            goal="Release 50 units from the escrow account to settle order #A17.",
            prereq_op=DEPOSIT_OP,
            settle_op=WITHDRAW_OP,
            amount=50,
        ),
        MultiStepTask(
            goal="Pay out 120 units from escrow to complete settlement #B44.",
            prereq_op=DEPOSIT_OP,
            settle_op=WITHDRAW_OP,
            amount=120,
        ),
    ]


def escrow_client(base_url: str = "https://escrow.example.com") -> AgentApiClient:
    """A recorded/probe-only client over the synthetic escrow surface. No live transport is
    wired, so there is nothing that COULD reach the wire — the probe guarantee by construction."""
    return AgentApiClient(ESCROW_SPEC, base_url=base_url)


def escrow_operations() -> dict[str, Operation]:
    return {op.operation_id: op for op in extract_operations(ESCROW_SPEC)}


# --- the injected LLM seam + the per-episode probe dispatcher ---------------------------

SYSTEM = (
    "You are an agent that completes a task by calling API tools. Read the tool "
    "descriptions, call the tools needed to accomplish the user's goal, and place each "
    "value into the parameter it belongs to. If a tool call fails, inspect the returned "
    "error and adjust your approach, then retry — do not give up after one failure. When "
    "the goal is fully accomplished, reply with a short confirmation and stop."
)


class LLM(Protocol):
    """The minimal Anthropic surface the loop touches (a real client or a scripted fake)."""

    messages: Any


def arm_tool_defs(
    ops: dict[str, Operation], *, include_query_docs: bool
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build the presented Anthropic tool defs for an arm and the set of presented NAMES.

    Both arms present the callable escrow tools (question-shaped, auth-hidden via ``to_tool``);
    only the self-heal arm additionally presents ``query_docs``. The name set is what the
    Phase-0 ``hallucination_rate`` checks a pick against (a pick outside it is an invented op).
    """
    from .tools import to_tool, tool_name

    defs: list[dict[str, Any]] = []
    names: set[str] = set()
    for op in ops.values():
        t = to_tool(op)
        name = tool_name(op)
        names.add(name)
        defs.append(
            {
                "name": name,
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema", {"type": "object"}),
            }
        )
    if include_query_docs:
        names.add("query_docs")
        defs.append(
            {
                "name": "query_docs",
                "description": (
                    "Search the API's virtualized docs to understand WHY a call failed and "
                    "how to rewrite it. Returns spec-derived doc snippets + the relevant "
                    "tool's inputSchema."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "What you were trying to do or the error you hit.",
                        }
                    },
                    "required": ["intent"],
                },
            }
        )
    return defs, names


@dataclass(frozen=True)
class ToolReply:
    """One dispatched tool result: the JSON ``content`` fed back to the model, the probe
    ``status`` (None for ``query_docs``), the op name invoked (None for docs/unknown), and
    whether the name was actually presented (``known``: a False here is a hallucination)."""

    content: str
    status: int | None
    op_name: str | None
    known: bool


class ProbeDispatcher:
    """Executes an agent's tool call against the offline sandbox for ONE episode.

    Owns a single per-episode :class:`SimWorld` (the state gate) so deposit -> withdraw
    correlate within the run and never leak across runs/arms. ``reveal_remediation`` gates
    whether a failed call returns the ``signals``/``remediation`` self-heal payload (self-heal
    arm) or the API's error body alone (baseline). Never calls ``client.call`` / the wire —
    ``sandbox.evaluate`` is the transport edge, so probe stays $0/synthetic by construction.
    """

    def __init__(
        self,
        client: AgentApiClient,
        ops: dict[str, Operation],
        presented: set[str],
        *,
        reveal_remediation: bool,
        session_id: str = "selfheal",
        now: float = 0.0,
    ) -> None:
        self._client = client
        self._ops = ops
        self._presented = presented
        self._reveal = reveal_remediation
        self._world = SimStore().get_or_create(session_id, now=now)

    def call(self, name: str, args: dict[str, Any]) -> ToolReply:
        if name == "query_docs":
            known = "query_docs" in self._presented
            docs = search_docs(self._client, str(args.get("intent", "")))
            return ToolReply(json.dumps(docs, default=str), None, None, known)

        op = self._ops.get(name)
        if op is None:
            # An invented / unpresented tool name — the Phase-0 hallucination signal.
            return ToolReply(
                json.dumps({"error": f"unknown tool: {name}"}),
                None,
                None,
                name in self._presented,
            )

        sim = evaluate(op, args, world=self._world)
        if self._reveal:
            body = {
                "status": sim.status,
                "data": sim.data,
                "signals": sim.signals,
                "remediation": sim.remediation,
                "mode_note": sim.mode_note,
            }
        else:
            # Baseline: the API's own error/success body ONLY — no signals, no remediation,
            # no self-heal note. The agent is told THAT it failed, never WHY.
            body = {"status": sim.status, "data": sim.data}
        return ToolReply(json.dumps(body, default=str), sim.status, name, True)


# --- one episode (a bounded tool-use loop) + scoring -----------------------------------


@dataclass(frozen=True)
class EpisodeOutcome:
    """One (task, arm, run) result — control-plane clean (booleans + counts, no arg values).

    ``multi_step_ok`` is the headline: the correlated sequence completed (``settle_op`` 200'd,
    which the SimWorld allows only after ``prereq_op`` funded it). ``heal_iter`` is the loop
    pass at which that first success landed (iterations-to-heal). ``healed`` means it succeeded
    AFTER at least one failed probe — a genuine recovery, not a lucky first ordering."""

    multi_step_ok: bool
    heal_iter: int | None
    tool_calls: int
    hallucinated: bool
    healed: bool


def _blocks(resp: Any) -> list[Any]:
    return list(getattr(resp, "content", None) or [])


def run_episode(
    llm: LLM,
    dispatcher: ProbeDispatcher,
    tool_defs: list[dict[str, Any]],
    prompt: str,
    settle_op: str,
    *,
    model: str,
    max_iters: int = 6,
    max_tokens: int = 1024,
) -> EpisodeOutcome:
    """Run the bounded tool-use loop for one task on one arm; return the outcome.

    The loop mirrors the Anthropic manual agentic pattern: send the goal + tools, execute
    every requested tool call through the sandbox dispatcher (blocks within a turn run IN
    ORDER against the same SimWorld, so a deposit-then-withdraw in one turn correlates), feed
    results back, and stop when the model ends its turn OR the correlated settle succeeds.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tool_calls = 0
    heal_iter: int | None = None
    failed_before = False
    hallucinated = False

    for i in range(1, max_iters + 1):
        resp = llm.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM,
            tools=tool_defs,
            messages=messages,
        )
        if getattr(resp, "stop_reason", None) != "tool_use":
            break
        messages.append({"role": "assistant", "content": _blocks(resp)})
        results: list[dict[str, Any]] = []
        for block in _blocks(resp):
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_calls += 1
            name = getattr(block, "name", "") or ""
            args = getattr(block, "input", None)
            reply = dispatcher.call(name, dict(args) if isinstance(args, dict) else {})
            if not reply.known:
                hallucinated = True
            if reply.status == 422:
                failed_before = True
            if reply.op_name == settle_op and reply.status == 200 and heal_iter is None:
                heal_iter = i
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": getattr(block, "id", f"tu-{tool_calls}"),
                    "content": reply.content,
                }
            )
        messages.append({"role": "user", "content": results})
        if heal_iter is not None:
            break  # correlated goal reached — stop spending

    multi_step_ok = heal_iter is not None
    return EpisodeOutcome(
        multi_step_ok=multi_step_ok,
        heal_iter=heal_iter,
        tool_calls=tool_calls,
        hallucinated=hallucinated,
        healed=multi_step_ok and failed_before,
    )


def build_prompt(task: MultiStepTask) -> str:
    return f"{task.goal}\n\nAmount involved: {task.amount}."


# --- one run record + aggregation (pure, testable; reuses the Phase-0 metric) ----------


@dataclass(frozen=True)
class SelfHealRecord:
    """One (task, arm, run) row. Carries ``arm`` + ``hallucinated`` so the Phase-0
    ``fcc_eval.hallucination_rate`` scores it directly (the reused metric)."""

    goal: str
    arm: str
    run: int
    multi_step_ok: bool
    heal_iter: int | None
    tool_calls: int
    hallucinated: bool
    healed: bool


ARMS: tuple[tuple[str, bool], ...] = (("baseline", False), ("selfheal", True))


def run_eval(
    llm: LLM,
    client: AgentApiClient,
    ops: dict[str, Operation],
    tasks: list[MultiStepTask],
    *,
    model: str,
    n_runs: int = 3,
    max_iters: int = 6,
    max_tokens: int = 1024,
) -> list[SelfHealRecord]:
    """Run both arms over every task ``n_runs`` times (Haiku is non-deterministic). Each
    episode gets a FRESH per-episode SimWorld (no cross-run/arm state), so the only thing
    that varies between arms is: query_docs presented? and remediation revealed?"""
    records: list[SelfHealRecord] = []
    for task in tasks:
        for arm, reveal in ARMS:
            defs, presented = arm_tool_defs(ops, include_query_docs=reveal)
            for run in range(n_runs):
                dispatcher = ProbeDispatcher(
                    client,
                    ops,
                    presented,
                    reveal_remediation=reveal,
                    session_id=f"{arm}-{task.goal[:8]}-{run}",
                )
                outcome = run_episode(
                    llm,
                    dispatcher,
                    defs,
                    build_prompt(task),
                    task.settle_op,
                    model=model,
                    max_iters=max_iters,
                    max_tokens=max_tokens,
                )
                records.append(
                    SelfHealRecord(
                        goal=task.goal,
                        arm=arm,
                        run=run,
                        multi_step_ok=outcome.multi_step_ok,
                        heal_iter=outcome.heal_iter,
                        tool_calls=outcome.tool_calls,
                        hallucinated=outcome.hallucinated,
                        healed=outcome.healed,
                    )
                )
    return records


def multi_step_success_rate(records: list[SelfHealRecord], arm: str) -> float:
    rows = [r for r in records if r.arm == arm]
    return sum(r.multi_step_ok for r in rows) / len(rows) if rows else 0.0


def heal_rate(records: list[SelfHealRecord], arm: str) -> float:
    """Fraction of an arm's episodes that succeeded AFTER a failed probe — a genuine recovery."""
    rows = [r for r in records if r.arm == arm]
    return sum(r.healed for r in rows) / len(rows) if rows else 0.0


def mean_iters_to_heal(records: list[SelfHealRecord], arm: str) -> float | None:
    """Mean loop-pass count to the first correlated success, over an arm's SUCCESSFUL episodes.
    ``None`` when the arm never completed the flow (no iters-to-heal to report)."""
    iters = [r.heal_iter for r in records if r.arm == arm and r.heal_iter is not None]
    return statistics.fmean(iters) if iters else None


def success_variance(records: list[SelfHealRecord], arm: str) -> tuple[float, float]:
    """(mean, stdev) of the per-run multi-step success rate for an arm across the N runs."""
    rows = [r for r in records if r.arm == arm]
    runs = sorted({r.run for r in rows})
    rates: list[float] = []
    for i in runs:
        run_rows = [r for r in rows if r.run == i]
        if run_rows:
            rates.append(sum(r.multi_step_ok for r in run_rows) / len(run_rows))
    if not rates:
        return 0.0, 0.0
    return statistics.fmean(rates), (statistics.stdev(rates) if len(rates) > 1 else 0.0)


def multi_step_lift(records: list[SelfHealRecord]) -> float:
    """The number that matters: SELF-HEAL multi-step success − BASELINE. Zero/negative is a
    real finding, not a bug — the self-heal loop did not measurably teach the flow."""
    return multi_step_success_rate(records, "selfheal") - multi_step_success_rate(
        records, "baseline"
    )
