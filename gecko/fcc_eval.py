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

The LLM is an injected seam (the Anthropic ``messages.create`` shape), so the whole
harness is offline-mockable — tests drive it with a
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
    """The number that matters: Gecko positive-FCC − raw positive-FCC (comprehension lift).

    POOLED over every fixture in ``records``. When the set spans APIs of unlike difficulty
    this averages them and then describes none of them: on the pinned run the pooled +0.30
    is the mean of a +0.55 API and a +0.00 API. Publish ``per_api_lift`` instead."""
    return fcc_rate(records, "gecko") - fcc_rate(records, "raw")


def lift_corpus(records: list[RunRecord]) -> float:
    """The flywheel number: gecko_corpus positive-FCC − gecko positive-FCC (corpus lift).

    Isolates what the CAPTURED corrections add on top of comprehension alone — the proof that
    a call's failure teaches the next agent to call it right. Zero when no correction matched
    any surfaced tool (a real finding, not a bug)."""
    return fcc_rate(records, "gecko_corpus") - fcc_rate(records, "gecko")


# --------------------------------------------------------------------------- #
# per-API lift — the ONLY publishable before/after pair
# --------------------------------------------------------------------------- #
#: The provenance label every block below carries. ``benchmark`` = a controlled two-arm
#: run over a committed golden set. It is NOT ``observed``: production data has ONE arm
#: (every call was prepared by Gecko), so it can certify a single-arm rate and can never
#: produce a before/after pair. Anything sourced from the corpus must use
#: ``telemetry.per_surface_fcc`` and its ``observed-with-gecko`` arm label instead.
BENCHMARK_PROVENANCE = "benchmark"

#: Spelled out rather than ``__name__``: under ``python -m`` the module name is
#: ``__main__``, and a block that mislabels its own producer is worse than an unlabeled one.
_MODULE = "gecko.fcc_eval"

#: The floor under EACH arm's denominator. A rate over ``d`` attempts has resolution
#: ``1/d``; publishing a two-digit percentage claim therefore needs ``1/d <= 0.10``, i.e.
#: ``d >= 10``. Below it a single flip moves the headline by ten points or more, so the
#: function refuses rather than emitting a number that a rerun would not reproduce.
MIN_ATTEMPTS_PER_ARM = 10

#: Closed vocabulary — like ``corpus.ERROR_CLASSES``, a refusal must name itself from a
#: fixed set so "we could not answer" cannot be silently re-styled into a finding.
RefusalReason = Literal["arm_absent", "below_floor", "unpaired"]


@dataclass(frozen=True)
class ArmRate:
    """One arm's positive-FCC rate on ONE api, carrying its own denominator."""

    arm: str
    attempts: int  # positive-task attempts = tasks x runs
    first_call_correct: int
    rate: float


@dataclass(frozen=True)
class ApiLift:
    """The before/after pair for ONE api. Emitted only when both arms cleared the floor
    on the SAME tasks — otherwise the api appears in ``PerApiLift.unscored`` instead."""

    api: str
    baseline: ArmRate
    treatment: ArmRate
    lift: float  # treatment.rate - baseline.rate, as a fraction
    tasks: int  # distinct positive tasks each arm ran
    runs: int
    provenance: str
    produced_by: str

    def sentence(self) -> str:
        """The publishable sentence, RENDERED FROM THESE FIELDS.

        Standing FEEDBACK: three published numbers once measured something other than the
        sentence printed around them. So the sentence is not written by hand next to the
        number — it is generated from the same object a test asserts, and it names both
        arms, both denominators, the provenance and the producing function."""
        return (
            f"{self.api}: {round(self.baseline.rate * 100)}% -> "
            f"{round(self.treatment.rate * 100)}% first-call-correct "
            f"({round(self.lift * 100):+d} pts), "
            f"{self.baseline.arm} vs {self.treatment.arm}, "
            f"{self.baseline.attempts} attempts per arm "
            f"({self.tasks} golden tasks x {self.runs} runs) "
            f"[{self.provenance}; {self.produced_by}]"
        )


@dataclass(frozen=True)
class UnscoredApi:
    """An api the function REFUSED to score, and why. Its existence is the point: the
    alternative is emitting ``0.0``, which reads as "measured, and Gecko did not help"."""

    api: str
    reason: RefusalReason
    baseline_attempts: int
    treatment_attempts: int
    minimum_attempts_per_arm: int
    provenance: str
    produced_by: str


@dataclass(frozen=True)
class PerApiLift:
    """The per-api split of the comprehension lift over a benchmark run.

    Reporting convention shared with ``telemetry.per_surface_fcc`` (same shape, same
    words) so the two published surfaces cannot drift: every block names its provenance,
    its denominator and the function that produced it; a thing that could not answer is
    absent from ``entries`` rather than present with a zero. One deliberate divergence —
    the telemetry block COUNTS suppressed surfaces without naming them, because a
    consumer's surface ids name their vendors. Here the api ids are OUR OWN committed
    fixtures, so refusals are named: nothing about them is a disclosure."""

    entries: tuple[ApiLift, ...]  # scored apis, biggest denominator first
    unscored: tuple[UnscoredApi, ...]  # refusals, by api id
    baseline_arm: str
    treatment_arm: str
    attempts: int  # block denominator: both arms' attempts across scored entries
    minimum_attempts_per_arm: int
    provenance: str
    produced_by: str

    def get(self, api: str) -> ApiLift | None:
        for entry in self.entries:
            if entry.api == api:
                return entry
        return None

    def lift_for(self, api: str) -> float | None:
        """The lift, or ``None`` for COULD NOT ANSWER — deliberately distinct from ``0.0``.

        ``0.0`` is a finding (both arms measured, the shaping bought nothing on this API —
        which is the true, published result for a clean well-documented one). ``None`` is
        silence (thin denominator, missing arm, unpaired arms, or an api not in the run).
        Collapsing the two turns "we did not measure" into "we measured no benefit"."""
        entry = self.get(api)
        return None if entry is None else entry.lift

    def refusal_for(self, api: str) -> RefusalReason | None:
        """Why ``api`` was not scored, or ``None`` if it was scored OR never appeared."""
        for row in self.unscored:
            if row.api == api:
                return row.reason
        return None


def _tasks(rows: list[RunRecord]) -> dict[tuple[str, str], int]:
    """The multiset of positive tasks an arm actually ran: (archetype, goal) -> attempts."""
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r.archetype, r.goal)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _arm_rate(arm: str, rows: list[RunRecord]) -> ArmRate:
    correct = sum(1 for r in rows if r.fcc)
    total = len(rows)
    return ArmRate(
        arm=arm,
        attempts=total,
        first_call_correct=correct,
        rate=round(correct / total, 4) if total else 0.0,
    )


def per_api_lift(
    records: list[RunRecord],
    *,
    baseline_arm: str = "raw",
    treatment_arm: str = "gecko",
) -> PerApiLift:
    """Split the comprehension lift by api — the only honest before/after pair we publish.

    **Why this function exists.** A "60 -> 93"-shaped claim needs two arms measured on the
    same tasks. That exists at exactly one seam: this golden-set harness, where ``raw``
    (the naive whole-spec dump) and ``gecko`` (search-surfaced, question-shaped,
    auth-hidden) answer identical tasks. Production/corpus data has NO control arm — every
    recorded call was prepared by Gecko — so it can certify a single-arm rate
    (``telemetry.per_surface_fcc``) and must never be differenced into a lift. Hence the
    fixed ``benchmark`` provenance on every block here.

    **Why per-api and not pooled.** ``lift()`` pools every fixture, so it reports the mean
    of unlike APIs and describes none of them; the difficulty of the API is the dominant
    variable in this metric, not the shaping. Per-api is also the honest form of the claim
    ("no lift on a clean API" is a result we publish, not one we hide in an average).

    **Refuses to score** rather than emit a number, per api, when:
      - ``arm_absent``  — an arm produced no positive rows for that api;
      - ``below_floor`` — either arm's denominator is under ``MIN_ATTEMPTS_PER_ARM``;
      - ``unpaired``    — the arms did not run the same tasks the same number of times,
        so the difference is a difference of task mixes, not of arms.
    A refused api is ABSENT from ``entries`` and present in ``unscored``; ``lift_for``
    returns ``None``. ``None`` is could-not-answer, ``0.0`` is a measured zero.

    **What this supersedes.** These per-API FCC figures are currently HAND-COPIED from a
    pinned run write-up into decks/docs and are superseded by this function's return
    value, which reproduces them exactly from the run's records:
      - TxODDS ``RAW 0.10 -> GECKO 0.65`` (lift +0.55, 120 positive attempts per arm)
      - Pegana ``RAW 1.00 -> GECKO 1.00`` (lift +0.00, 100 positive attempts per arm)
      - the POOLED ``RAW 0.51 -> GECKO 0.81`` (+0.30) headline, which is retired for
        publication: it is the average of the two rows above and equals neither.
    Earlier unpinned variants of the same claim ("~6x", "8 -> 61%", "0.44") were already
    superseded by that write-up and are superseded again here.

    **What this does NOT supersede — do not re-attribute these to this function.** The
    canonical RETRIEVAL figures are ``recall@1 0.74 | recall@3 0.89 | MRR 0.81`` over 27
    wired-gold rows, produced by ``gecko.retrieval_eval.evaluate_golden()``. They measure
    the router (did we surface the right operation), not whether an agent then called it
    correctly, and they have no second arm. A ``0.78`` is circulating for that metric: it
    is not this function's output, it is not any figure in the canonical set, and nothing
    here certifies it.

    Pure function of ``records``: no I/O, no network, no state. Reads booleans and names
    only — control-plane clean, like every other aggregator in this module."""
    produced_by = f"{_MODULE}.per_api_lift"
    pos = positive(records)
    by_api: dict[str, dict[str, list[RunRecord]]] = {}
    for r in pos:
        if r.arm in (baseline_arm, treatment_arm):
            by_api.setdefault(r.fixture, {}).setdefault(r.arm, []).append(r)

    entries: list[ApiLift] = []
    unscored: list[UnscoredApi] = []
    for api in sorted(by_api):
        arms = by_api[api]
        base_rows = arms.get(baseline_arm, [])
        treat_rows = arms.get(treatment_arm, [])
        base = _arm_rate(baseline_arm, base_rows)
        treat = _arm_rate(treatment_arm, treat_rows)

        reason: RefusalReason | None = None
        if not base_rows or not treat_rows:
            reason = "arm_absent"
        elif min(base.attempts, treat.attempts) < MIN_ATTEMPTS_PER_ARM:
            reason = "below_floor"
        elif _tasks(base_rows) != _tasks(treat_rows):
            # Same count, different tasks still fails: an arm that got an easier mix would
            # otherwise read as an arm that comprehended better.
            reason = "unpaired"
        if reason is not None:
            unscored.append(
                UnscoredApi(
                    api=api,
                    reason=reason,
                    baseline_attempts=base.attempts,
                    treatment_attempts=treat.attempts,
                    minimum_attempts_per_arm=MIN_ATTEMPTS_PER_ARM,
                    provenance=BENCHMARK_PROVENANCE,
                    produced_by=produced_by,
                )
            )
            continue

        tasks = _tasks(base_rows)
        entries.append(
            ApiLift(
                api=api,
                baseline=base,
                treatment=treat,
                lift=round(treat.rate - base.rate, 4),
                tasks=len(tasks),
                runs=len({r.run for r in base_rows}),
                provenance=BENCHMARK_PROVENANCE,
                produced_by=produced_by,
            )
        )

    # Deterministic: biggest denominator first, ties by api id — the split leads with the
    # api whose evidence is strongest, not the one whose number is largest.
    entries.sort(key=lambda e: (-(e.baseline.attempts + e.treatment.attempts), e.api))
    return PerApiLift(
        entries=tuple(entries),
        unscored=tuple(unscored),
        baseline_arm=baseline_arm,
        treatment_arm=treatment_arm,
        attempts=sum(e.baseline.attempts + e.treatment.attempts for e in entries),
        minimum_attempts_per_arm=MIN_ATTEMPTS_PER_ARM,
        provenance=BENCHMARK_PROVENANCE,
        produced_by=produced_by,
    )
