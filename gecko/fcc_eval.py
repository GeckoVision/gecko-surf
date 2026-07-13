"""Agent-in-the-loop first-call-correct (FCC) eval — the companion metric to the
golden retrieval eval (semantic-catalog plan §2 note).

**What this measures — read this before quoting a number.** This is the *comprehension*
lift: Gecko's question-shaped, auth-hidden, retrieval-surfaced tools vs the naive
"dump every OpenAPI operation as a tool" a DIY builder / coding-agent one-shot produces.
It is NOT the accumulated-*corpus* lift — no contributed correctness corpus exists yet.
If the edge is thin on a well-documented API, that is a real finding, not a bug.

Two arms, same goal, same cheap model (Haiku), one tool-use turn each:
  - **RAW**   — every operation dumped verbatim: raw operationId, raw summary+description,
                ALL params incl. auth headers (NOT hidden), auth params still ``required``.
  - **GECKO** — ``client.search(goal)`` → top-k question-shaped, auth-hidden tool defs.

Scored per task (positive tasks):
  ``tool_correct`` = picked ∈ ``expect_ops``
  ``well_formed``  = ``build_request(picked, agent_args)`` does not raise (the caller guard)
  ``args_match``   = every gold-required param supplied with a right-KIND value and the
                     disambiguator routed correctly (a mint value as ``mint`` not ``symbol``)
  ``fcc``          = all three.
Out-of-scope tasks (``expect_ops == []``) are correct iff the agent declines (no tool call).

Control-plane discipline: records only metadata — picked tool name, the boolean outcomes,
and the *shape* of args (param name -> value-KIND). Never a response payload; never a raw
arg value beyond the kind used for the disambiguation check.

The LLM is an injected seam (the Anthropic ``messages.create`` shape reused from
``examples/sos_vzla_bot``), so the whole harness is offline-mockable — tests drive it with a
scripted fake; only the runner talks to Haiku.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from .corrections import Correction

from .caller import CallError, build_request
from .client import AgentApiClient
from .evaluate import GoldenTask
from .ingest import Operation
from .tools import _body_schema, tool_name

Arm = Literal["raw", "gecko", "gecko_corpus"]
ValueKind = Literal["int", "mint", "symbol", "float", "bool", "none", "other"]

# Base58 (Bitcoin/Solana) alphabet — no 0, O, I, l. A Solana mint is 32-44 of these.
_B58 = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_MINT_MIN, _MINT_MAX = 32, 44
# Cap the untrusted raw description before it reaches the model (rules/python.md).
_RAW_DESC_CAP = 500


# --- the disambiguation-aware arg check (the thing a golden-args harness is blind to) ---


def _is_mint_shaped(s: str) -> bool:
    return _MINT_MIN <= len(s) <= _MINT_MAX and all(c in _B58 for c in s)


def value_kind(v: Any) -> ValueKind:
    """Classify a value into the KIND that matters for routing an identifier.

    The load-bearing distinction is ``mint`` (a long base58 address) vs ``symbol`` (a short
    ticker) vs ``int`` (a numeric id): the same natural-language slot ("this asset") can be
    either, and comprehension is what routes it to the right parameter. ``bool`` before
    ``int`` because ``bool`` is an ``int`` subclass in Python."""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return "none"
        if _is_mint_shaped(s):
            return "mint"
        if s.lstrip("-").isdigit():
            return "int"
        return "symbol"
    if v is None:
        return "none"
    return "other"


def arg_shape(args: Mapping[str, Any]) -> dict[str, str]:
    """The control-plane-safe projection of an arg dict: name -> value-KIND, never values."""
    return {str(k): value_kind(v) for k, v in args.items()}


def args_match(gold_args: Mapping[str, Any], agent_args: Mapping[str, Any]) -> bool:
    """True iff every gold-required param is supplied under the SAME name with a same-KIND
    value. This catches the mint-vs-symbol gotcha in both directions:
      - routing a mint value to ``symbol`` (or vice-versa) -> the gold key is absent -> False
      - supplying the right key but a wrong-kind value (``mint="jitoSOL"``) -> kind ≠ -> False
    No gold params (streams, list-all) -> vacuously True. Extra agent params are ignored."""
    for name, gold_val in gold_args.items():
        if name not in agent_args:
            return False
        if value_kind(agent_args[name]) != value_kind(gold_val):
            return False
    return True


# --- the two arms' tool defs -----------------------------------------------------------


@dataclass(frozen=True)
class ArmTool:
    """One presented tool, in both the shape the model sees (``anthropic``) and the shape
    the caller guard builds a request from (``caller``). ``name`` is the sanitized
    operationId in BOTH arms — the same choice a naive builder must make for API validity,
    so it is not a Gecko-only advantage; the arms differ in description + params, not name."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    invoke: Mapping[str, Any]

    def anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }

    def caller(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inputSchema": dict(self.input_schema),
            "_invoke": dict(self.invoke),
        }


def _raw_description(op: Operation) -> str:
    """The naive dump's description: raw summary + raw description, NO question-shaping
    (no 'Required:/Optional:' hints, no reframing). Capped for prompt economy + safety."""
    text = " ".join(p for p in (op.summary, op.description) if p).strip()
    return (text or op.operation_id)[:_RAW_DESC_CAP]


def _raw_input_schema(op: Operation) -> dict[str, Any]:
    """Every parameter, auth NOT hidden, ``required`` carried through verbatim — the schema
    a DIY OpenAPI-to-tools dump produces. This is where auth-hiding earns its keep: a raw
    op that marks ``Authorization``/``X-Api-Token`` required forces the agent to satisfy
    plumbing it should never see."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in op.parameters:
        schema = dict(p.schema) if isinstance(p.schema, dict) else {}
        if p.description and "description" not in schema:
            schema["description"] = p.description
        props[p.name] = schema
        if p.required:
            required.append(p.name)
    body_schema, body_required = _body_schema(op)
    if body_schema is not None:
        props["body"] = body_schema
        if body_required:
            required.append("body")
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def raw_tools(operations: list[Operation]) -> list[ArmTool]:
    """RAW arm: the whole spec dumped, every op a tool, nothing hidden or shaped."""
    out: list[ArmTool] = []
    for op in operations:
        invoke = {
            "method": op.method,
            "path": op.path,
            "param_locations": {p.name: p.location for p in op.parameters},
        }
        out.append(
            ArmTool(tool_name(op), _raw_description(op), _raw_input_schema(op), invoke)
        )
    return out


def gecko_tools(client: AgentApiClient, goal: str, k: int) -> list[ArmTool]:
    """GECKO arm: search-surfaced, question-shaped, auth-hidden top-k tool defs."""
    by_name = {t["name"]: t for t in client.list_tools()}
    out: list[ArmTool] = []
    for hit in client.search(goal, limit=k):
        t = by_name.get(hit["name"])
        if t is None:
            continue
        out.append(ArmTool(t["name"], t["description"], t["inputSchema"], t["_invoke"]))
    return out


def gecko_corpus_tools(
    client: AgentApiClient, goal: str, k: int, corrections: list[Correction]
) -> list[ArmTool]:
    """GECKO+CORPUS arm: the GECKO tools, each enriched with any matching captured
    corrections (a ``Correctness note:`` line re-injected into the description + param schema).

    This is the flywheel's output presented back to the agent: comprehension PLUS what prior
    failures taught about calling the op right. Identical to ``gecko_tools`` when no correction
    matches, so any FCC delta is attributable to the corrections alone."""
    from .corrections import enrich_with_corrections  # local: avoid import cycle

    out: list[ArmTool] = []
    for t in gecko_tools(client, goal, k):
        td = {
            "name": t.name,
            "description": t.description,
            "inputSchema": dict(t.input_schema),
        }
        enriched = enrich_with_corrections(td, corrections)
        out.append(
            ArmTool(t.name, enriched["description"], enriched["inputSchema"], t.invoke)
        )
    return out


# --- the injected LLM seam (Anthropic messages shape) + one pick turn -------------------

SYSTEM = (
    "You are an API-calling agent. The user states a goal and may provide concrete "
    "value(s) to use. Choose the SINGLE most appropriate tool and call it, placing each "
    "provided value into the parameter it belongs to based on the tool's schema and "
    "description. If NONE of the available tools can serve the goal, do NOT call any "
    "tool — reply with a brief text note instead."
)


class LLM(Protocol):
    """The minimal Anthropic surface the pick turn touches (a real client, an OpenRouter
    adapter, or a scripted fake all satisfy this)."""

    messages: Any


def build_prompt(task: GoldenTask) -> str:
    """Goal + the concrete gold value(s) — values ONLY, never their parameter names, so the
    model must decide *where* each value goes. This is what makes the mint-vs-symbol routing
    observable: hand it a base58 string and a ticker and see which parameter it fills."""
    prompt = task.goal
    if task.args:
        values = ", ".join(str(v) for v in task.args.values())
        prompt += f"\n\nUse this value where appropriate: {values}"
    return prompt


def pick(
    llm: LLM,
    *,
    model: str,
    tools: list[dict[str, Any]],
    prompt: str,
    max_tokens: int = 1024,
) -> tuple[str | None, dict[str, Any]]:
    """One tool-use turn. Returns (picked tool name, emitted args), or (None, {}) if the
    model declined to call a tool (the correct move for an out-of-scope goal)."""
    resp = llm.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM,
        tools=tools,
        messages=[{"role": "user", "content": prompt}],
    )
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", None) == "tool_use":
            args = getattr(block, "input", None)
            emitted = dict(args) if isinstance(args, dict) else {}
            return getattr(block, "name", None), emitted
    return None, {}


# --- scoring + one run record ----------------------------------------------------------


@dataclass(frozen=True)
class FccScore:
    tool_correct: bool
    well_formed: bool
    args_match: bool
    fcc: bool
    hallucinated: bool = False


def score(
    task: GoldenTask,
    picked: str | None,
    agent_args: Mapping[str, Any],
    caller_tool: Mapping[str, Any] | None,
    base_url: str,
    presented: set[str] | None = None,
) -> FccScore:
    """Score one pick against the gold. Out-of-scope: correct iff the agent declined.

    ``hallucinated`` is True iff the model named a tool that was NOT among the ``presented``
    names for its arm (an invented op), independent of scope. A real-but-wrong pick (picked ∈
    presented but ∉ ``expect_ops``) is a miss, not a hallucination. When ``presented`` is
    unknown (None) it cannot be judged and stays False. Reads tool NAMES only — control-plane
    clean, never an arg value or payload."""
    hallucinated = (
        picked is not None and presented is not None and picked not in presented
    )
    if not task.expect_ops:
        declined = picked is None
        return FccScore(declined, declined, declined, declined, hallucinated)

    tool_correct = picked is not None and picked in task.expect_ops
    well_formed = False
    if caller_tool is not None:
        try:
            build_request(dict(caller_tool), dict(agent_args), base_url, auth=None)
            well_formed = True
        except CallError:
            well_formed = False
        except Exception:  # noqa: BLE001 - any other raise is still "not well-formed"
            well_formed = False
    matched = args_match(task.args, agent_args)
    fcc = tool_correct and well_formed and matched
    return FccScore(tool_correct, well_formed, matched, fcc, hallucinated)


@dataclass(frozen=True)
class RunRecord:
    """One (task, arm, run) outcome — control-plane clean (shapes + booleans, no values)."""

    fixture: str
    archetype: str
    goal: str
    arm: str
    run: int
    picked: str | None
    retrieval_hit: bool
    tool_correct: bool
    well_formed: bool
    args_match: bool
    fcc: bool
    gold_shape: Mapping[str, str] = field(default_factory=dict)
    agent_shape: Mapping[str, str] = field(default_factory=dict)
    hallucinated: bool = False


def evaluate_fcc(
    fixture: str,
    client: AgentApiClient,
    tasks: list[GoldenTask],
    llm: LLM,
    *,
    model: str,
    k: int = 8,
    n_runs: int = 3,
    max_tokens: int = 1024,
    corrections: list[Correction] | None = None,
) -> list[RunRecord]:
    """Run every arm over every task, ``n_runs`` times (Haiku is non-deterministic).

    RAW tools (the whole spec) are built once; GECKO tools are the per-goal search top-k.
    When ``corrections`` is supplied, a THIRD arm ``gecko_corpus`` runs identically — the
    GECKO tools enriched with the captured corrections — so any FCC delta over GECKO is
    attributable to the corpus alone (the flywheel lift). One LLM pick per (task, arm, run);
    scored with the shared caller guard + ``args_match``."""
    raw = raw_tools(client.operations)
    raw_anthropic = [t.anthropic() for t in raw]
    raw_caller = {t.name: t.caller() for t in raw}
    base_url = client.base_url

    records: list[RunRecord] = []
    for task in tasks:
        gk = gecko_tools(client, task.goal, k)
        gk_anthropic = [t.anthropic() for t in gk]
        gk_caller = {t.name: t.caller() for t in gk}
        expect = set(task.expect_ops)
        raw_hit = bool(expect) and any(t.name in expect for t in raw)
        gk_hit = bool(expect) and any(t.name in expect for t in gk)

        arms: list[
            tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]], bool]
        ] = [
            ("raw", raw_anthropic, raw_caller, raw_hit),
            ("gecko", gk_anthropic, gk_caller, gk_hit),
        ]
        if corrections is not None:
            gc = gecko_corpus_tools(client, task.goal, k, corrections)
            gc_anthropic = [t.anthropic() for t in gc]
            gc_caller = {t.name: t.caller() for t in gc}
            gc_hit = bool(expect) and any(t.name in expect for t in gc)
            arms.append(("gecko_corpus", gc_anthropic, gc_caller, gc_hit))

        for run in range(n_runs):
            for arm, anthropic_tools, caller_map, hit in arms:
                picked, agent_args = pick(
                    llm,
                    model=model,
                    tools=anthropic_tools,
                    prompt=build_prompt(task),
                    max_tokens=max_tokens,
                )
                s = score(
                    task,
                    picked,
                    agent_args,
                    caller_map.get(picked) if picked else None,
                    base_url,
                    presented=set(caller_map),
                )
                records.append(
                    RunRecord(
                        fixture=fixture,
                        archetype=task.archetype,
                        goal=task.goal,
                        arm=arm,
                        run=run,
                        picked=picked,
                        retrieval_hit=hit,
                        tool_correct=s.tool_correct,
                        well_formed=s.well_formed,
                        args_match=s.args_match,
                        fcc=s.fcc,
                        gold_shape=arg_shape(task.args),
                        agent_shape=arg_shape(agent_args),
                        hallucinated=s.hallucinated,
                    )
                )
    return records


# --- aggregation (pure, testable) ------------------------------------------------------


def _rate(records: list[RunRecord], predicate: Any) -> float:
    hits = [r for r in records if predicate(r)]
    return sum(r.fcc for r in hits) / len(hits) if hits else 0.0


def positive(records: list[RunRecord]) -> list[RunRecord]:
    return [r for r in records if r.archetype != "out_of_scope"]


def fcc_rate(records: list[RunRecord], arm: str) -> float:
    """Headline FCC rate for an arm over POSITIVE tasks (out-of-scope scored separately)."""
    return _rate(positive(records), lambda r: r.arm == arm)


def hallucination_rate(records: list[RunRecord], arm: str) -> float:
    """Fraction of an arm's picks that named a tool the arm never presented (an invented op).

    Denominator is every record for the arm (a pick or a decline); reads only the metadata
    ``hallucinated`` boolean — control-plane clean. Sibling of ``fcc_rate``."""
    rows = [r for r in records if r.arm == arm]
    return sum(r.hallucinated for r in rows) / len(rows) if rows else 0.0


def retrieval_recall_at_k(records: list[RunRecord], arm: str) -> float:
    """The retrieval CEILING for an arm: fraction of positive tasks whose gold op was in the
    surfaced top-k (``retrieval_hit``). This bounds achievable FCC — the model cannot pick an
    op it never saw — so it is the number to read before any generation tuning. Deduped per
    task (``retrieval_hit`` is run-invariant); reads booleans + names only."""
    by_task: dict[tuple[str, str, str], bool] = {}
    for r in positive(records):
        if r.arm == arm:
            by_task[(r.fixture, r.archetype, r.goal)] = r.retrieval_hit
    return sum(by_task.values()) / len(by_task) if by_task else 0.0


def per_archetype(records: list[RunRecord], arm: str) -> dict[str, float]:
    arches = sorted({r.archetype for r in records})
    return {
        a: _rate(records, lambda r, a=a: r.arm == arm and r.archetype == a)
        for a in arches
    }


def run_variance(records: list[RunRecord], arm: str) -> tuple[float, float]:
    """(mean, stdev) of the per-run positive-FCC rate for an arm across the N runs."""
    pos = positive(records)
    runs = sorted({r.run for r in pos})
    rates = [_rate(pos, lambda r, i=i: r.arm == arm and r.run == i) for i in runs]
    if not rates:
        return 0.0, 0.0
    return statistics.fmean(rates), (statistics.stdev(rates) if len(rates) > 1 else 0.0)


def lift(records: list[RunRecord]) -> float:
    """The number that matters: Gecko positive-FCC − raw positive-FCC (comprehension lift)."""
    return fcc_rate(records, "gecko") - fcc_rate(records, "raw")


def lift_corpus(records: list[RunRecord]) -> float:
    """The flywheel number: gecko_corpus positive-FCC − gecko positive-FCC (corpus lift).

    Isolates what the CAPTURED corrections add on top of comprehension alone — the proof that
    a call's failure teaches the next agent to call it right. Zero when no correction matched
    any surfaced tool (a real finding, not a bug)."""
    return fcc_rate(records, "gecko_corpus") - fcc_rate(records, "gecko")
