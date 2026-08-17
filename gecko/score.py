"""The provider report — one surface, before and after, and what changed.

WHAT THIS IS FOR. `gecko.fcc_eval` measures whether an agent makes the right call first
try, on two arms: the raw spec dump an agent actually gets today, and the comprehended
surface. It was built as an internal eval and it answers a provider's question exactly:

    your surface, as an agent finds it today   ->  60%
    your surface, comprehended                 ->  83%
                                                   and here is what changed

This module turns those run records into that report. It adds no measurement of its own —
every number here comes from `fcc_eval` — and it exists because an internal eval and a
provider artifact need different honesty.

THE FOUR THINGS IT REFUSES TO DO, each of which would make the number prettier:

1. **It will not pool across surfaces.** `fcc_eval.lift`'s own docstring records why: on a
   pinned run the pooled +0.30 was the mean of a +0.55 API and a +0.00 API, so it described
   neither. A provider report is about ONE surface; pooling is a category error here and
   raises rather than averages.
2. **It will not call a lift inside run-to-run noise an improvement.** The model is
   non-deterministic, so an arm's rate moves between runs on its own. A lift no larger than
   that movement is a measurement, not a result, and :attr:`SurfaceScore.lift_is_noise` says
   so before anyone quotes it.
3. **It reports regressions beside improvements.** A report that only lists what got better
   is marketing. If comprehension broke an intent that worked on the raw dump, that is the
   most useful line in the document.
4. **It reports what still fails.** An intent failing on BOTH arms is not a win to omit —
   it is the work remaining, and it is what a provider can act on this week.

WHAT A LIFT IS AND IS NOT. This is the COMPREHENSION lift: question-shaped, auth-hidden,
retrieval-surfaced tools against the raw dump. It is not an accumulated-corpus lift — no
contributed corpus exists — and a thin edge on a well-documented API is a real finding
about that API, not a failure of the measurement. The caveats travel inside
:class:`SurfaceScore` rather than only in prose, because a number gets copied out of a
document and its qualifications do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .fcc_eval import (
    RunRecord,
    fcc_rate,
    hallucination_rate,
    positive,
    retrieval_recall_at_k,
    run_variance,
)

__all__ = [
    "ArmScore",
    "Gates",
    "IntentDelta",
    "ScoreError",
    "SurfaceScore",
    "score_surface",
]

#: Below this, a change in an intent's pass rate is treated as unchanged rather than a
#: direction. With the usual three runs a single flipped run is 0.33, so anything under one
#: run's worth of movement cannot be told apart from the model's own variance.
_INTENT_EPSILON = 0.001


class ScoreError(RuntimeError):
    """The records cannot honestly produce a single surface's report."""


@dataclass(frozen=True)
class Gates:
    """The four gates a first-call-correct outcome passes through, so a lift is attributable.

    A headline that moves without any gate moving is an artefact. A headline that moves
    because ``retrieval`` moved is a different product claim from one that moves because
    ``args_match`` moved — the first says the agent could not FIND the call, the second says
    it found it and filled it in wrong.
    """

    retrieval: float
    tool_correct: float
    well_formed: float
    args_match: float

    def moved_against(self, other: "Gates") -> tuple[tuple[str, float], ...]:
        """Which gates changed versus ``other``, largest movement first."""
        deltas = (
            ("retrieval", self.retrieval - other.retrieval),
            ("tool_correct", self.tool_correct - other.tool_correct),
            ("well_formed", self.well_formed - other.well_formed),
            ("args_match", self.args_match - other.args_match),
        )
        moved = [(name, d) for name, d in deltas if abs(d) > _INTENT_EPSILON]
        return tuple(sorted(moved, key=lambda pair: -abs(pair[1])))


@dataclass(frozen=True)
class ArmScore:
    """One arm's numbers. ``arm`` is the raw dump or the comprehended surface."""

    arm: str
    fcc: float
    hallucination: float
    #: The retrieval CEILING — the model cannot pick a call it was never shown, so this
    #: bounds `fcc` and is the number to read before anything about generation.
    retrieval_ceiling: float
    gates: Gates
    run_mean: float
    run_stdev: float


@dataclass(frozen=True)
class IntentDelta:
    """One intent, phrased the way a user would ask, before and after.

    ``goal`` is deliberately the user's words rather than an operation id: a provider
    reading this needs to recognise the request, and "buy a bottle of water at the bar" is
    recognisable in a way ``make_purchase`` is not.
    """

    goal: str
    archetype: str
    before: float
    after: float

    @property
    def delta(self) -> float:
        return self.after - self.before

    @property
    def verdict(self) -> str:
        """``fixed`` · ``broke`` · ``still_failing`` · ``already_worked`` · ``unchanged``."""
        if self.delta > _INTENT_EPSILON:
            return "fixed"
        if self.delta < -_INTENT_EPSILON:
            return "broke"
        if self.after <= _INTENT_EPSILON:
            return "still_failing"
        if self.after >= 1.0 - _INTENT_EPSILON:
            return "already_worked"
        return "unchanged"


@dataclass(frozen=True)
class SurfaceScore:
    """One surface, both arms, and every claim this report is willing to make."""

    surface: str
    tasks: int
    runs: int
    before: ArmScore
    after: ArmScore
    intents: tuple[IntentDelta, ...]
    #: Out-of-scope tasks are scored apart from the headline: the right answer is to DECLINE,
    #: and folding a refusal into a success rate hides whether a surface knows its own edges.
    declined_before: tuple[int, int]
    declined_after: tuple[int, int]
    caveats: tuple[str, ...] = field(default_factory=tuple)

    @property
    def lift(self) -> float:
        return self.after.fcc - self.before.fcc

    @property
    def noise_floor(self) -> float:
        """The larger of the two arms' run-to-run movement.

        Using the LARGER of the two is the conservative choice on purpose: it is the amount
        of movement this measurement demonstrably produces on its own.
        """
        return max(self.before.run_stdev, self.after.run_stdev)

    @property
    def verdict(self) -> str:
        """``improved`` · ``regressed`` · ``no_difference`` · ``undetermined``.

        The distinction between the last two is not pedantry, and a real run is what
        surfaced it. Pegana scored 100% on BOTH arms across seven runs with zero variance:
        a lift of exactly zero, measured exactly. Calling that "undetermined, run more"
        told a provider to spend money re-measuring a settled answer. A lift of zero with a
        noise floor of zero is a RESULT — this surface needed nothing from us on this set —
        and saying so plainly is worth more than a hedge.
        """
        floor = self.noise_floor
        if floor == 0.0 and abs(self.lift) <= _INTENT_EPSILON:
            return "no_difference"
        if abs(self.lift) <= floor:
            return "undetermined"
        return "improved" if self.lift > 0 else "regressed"

    @property
    def lift_is_noise(self) -> bool:
        """True when a nonzero lift is no larger than the movement either arm shows unaided.

        A True here does not mean the surface did not improve. It means THIS RUN cannot tell,
        and the honest response is more runs, not a rounder number. A determined zero is NOT
        noise — see :attr:`verdict`.
        """
        return self.verdict == "undetermined"

    @property
    def fixed(self) -> tuple[IntentDelta, ...]:
        return tuple(i for i in self.intents if i.verdict == "fixed")

    @property
    def broke(self) -> tuple[IntentDelta, ...]:
        """Intents comprehension made WORSE. Reported first when non-empty."""
        return tuple(i for i in self.intents if i.verdict == "broke")

    @property
    def still_failing(self) -> tuple[IntentDelta, ...]:
        """Failing on both arms — the work remaining, and the most actionable list here."""
        return tuple(i for i in self.intents if i.verdict == "still_failing")

    @property
    def gates_moved(self) -> tuple[tuple[str, float], ...]:
        return self.after.gates.moved_against(self.before.gates)


def _gates(records: Sequence[RunRecord], arm: str) -> Gates:
    rows = [r for r in positive(list(records)) if r.arm == arm]
    n = len(rows) or 1
    return Gates(
        retrieval=sum(r.retrieval_hit for r in rows) / n,
        tool_correct=sum(r.tool_correct for r in rows) / n,
        well_formed=sum(r.well_formed for r in rows) / n,
        args_match=sum(r.args_match for r in rows) / n,
    )


def _arm(records: Sequence[RunRecord], arm: str) -> ArmScore:
    rows = list(records)
    mean, stdev = run_variance(rows, arm)
    return ArmScore(
        arm=arm,
        fcc=fcc_rate(rows, arm),
        hallucination=hallucination_rate(rows, arm),
        retrieval_ceiling=retrieval_recall_at_k(rows, arm),
        gates=_gates(rows, arm),
        run_mean=mean,
        run_stdev=stdev,
    )


def _declined(records: Sequence[RunRecord], arm: str) -> tuple[int, int]:
    rows = [r for r in records if r.archetype == "out_of_scope" and r.arm == arm]
    return sum(r.fcc for r in rows), len(rows)


def _intents(
    records: Sequence[RunRecord], *, before: str, after: str
) -> tuple[IntentDelta, ...]:
    """Per-intent pass rates for both arms, over positive tasks only."""
    seen: dict[tuple[str, str], dict[str, list[bool]]] = {}
    for record in positive(list(records)):
        if record.arm not in (before, after):
            continue
        key = (record.goal, record.archetype)
        seen.setdefault(key, {}).setdefault(record.arm, []).append(record.fcc)

    out: list[IntentDelta] = []
    for (goal, archetype), arms in seen.items():
        before_runs = arms.get(before, [])
        after_runs = arms.get(after, [])
        if not before_runs or not after_runs:
            # An intent only one arm attempted cannot produce a before/after. Dropping it
            # silently would let an asymmetric task list flatter whichever arm ran it.
            continue
        out.append(
            IntentDelta(
                goal=goal,
                archetype=archetype,
                before=sum(before_runs) / len(before_runs),
                after=sum(after_runs) / len(after_runs),
            )
        )
    # Regressions first, then the biggest wins: a reader who stops after three lines should
    # have seen the bad news.
    return tuple(sorted(out, key=lambda i: (i.delta, i.goal)))


def _caveats(score_runs: int, verdict: str, ceiling_bound: bool) -> tuple[str, ...]:
    out = [
        "This is the COMPREHENSION lift — question-shaped, auth-hidden, retrieval-surfaced "
        "tools against the raw spec dump an agent gets today. It is not a corpus lift; no "
        "contributed corpus exists. A thin edge on a well-documented surface is a real "
        "finding about that surface.",
        "The baseline arm is the raw specification, which is what an agent actually "
        "receives. It is not a strawman built to lose, and the number is only worth "
        "quoting while that stays true.",
    ]
    if score_runs < 3:
        out.append(
            f"Only {score_runs} run(s) per task. The model is non-deterministic, so a "
            "single run cannot separate a result from its own variance."
        )
    if verdict == "undetermined":
        out.append(
            "THE LIFT IS INSIDE THIS MEASUREMENT'S OWN RUN-TO-RUN MOVEMENT. Treat it as "
            "undetermined and run more, rather than reporting the midpoint."
        )
    if verdict == "no_difference":
        out.append(
            "BOTH ARMS SCORED THE SAME, EXACTLY, WITH NO RUN-TO-RUN MOVEMENT. This is a "
            "result and not a hedge: on these intents the raw specification already got "
            "every call right, so comprehension had nothing to add. Either the surface is "
            "genuinely that clear, or the intents are too close to its own wording to be a "
            "test — and the second is worth checking before the first is celebrated."
        )
    if ceiling_bound:
        out.append(
            "The comprehended arm's first-call rate is at its retrieval ceiling: every "
            "call the agent was shown, it got right. Further gain has to come from "
            "retrieval, not from generation."
        )
    return tuple(out)


def score_surface(
    records: Sequence[RunRecord],
    *,
    surface: str | None = None,
    before: str = "raw",
    after: str = "gecko",
) -> SurfaceScore:
    """Turn `fcc_eval` run records into ONE surface's provider report.

    ``surface`` defaults to the single fixture present in ``records``. Records spanning more
    than one fixture raise :class:`ScoreError` rather than pooling — see this module's
    docstring, and `fcc_eval.lift`, for the run where a pooled +0.30 was the mean of a
    +0.55 API and a +0.00 API and therefore described neither.
    """
    rows = list(records)
    if not rows:
        raise ScoreError("no run records — there is nothing to report")

    fixtures = sorted({r.fixture for r in rows})
    if len(fixtures) > 1:
        raise ScoreError(
            f"records span {len(fixtures)} surfaces ({', '.join(fixtures)}) and a provider "
            "report describes ONE. Pooling would average unlike surfaces and describe "
            "neither — score each separately."
        )
    resolved = surface or fixtures[0]

    arms = sorted({r.arm for r in rows})
    for needed in (before, after):
        if needed not in arms:
            raise ScoreError(
                f"arm {needed!r} is not in these records (found: {', '.join(arms)}) — "
                "a before/after report needs both arms measured on the same tasks"
            )

    before_score = _arm(rows, before)
    after_score = _arm(rows, after)
    intents = _intents(rows, before=before, after=after)
    runs = len({r.run for r in rows})

    partial = SurfaceScore(
        surface=resolved,
        tasks=len(intents),
        runs=runs,
        before=before_score,
        after=after_score,
        intents=intents,
        declined_before=_declined(rows, before),
        declined_after=_declined(rows, after),
    )
    ceiling_bound = (
        after_score.retrieval_ceiling > 0.0
        and after_score.fcc >= after_score.retrieval_ceiling - _INTENT_EPSILON
    )
    return SurfaceScore(
        surface=partial.surface,
        tasks=partial.tasks,
        runs=partial.runs,
        before=partial.before,
        after=partial.after,
        intents=partial.intents,
        declined_before=partial.declined_before,
        declined_after=partial.declined_after,
        caveats=_caveats(runs, partial.verdict, ceiling_bound),
    )


def render_report(
    score: SurfaceScore, *, gate_names: Mapping[str, str] | None = None
) -> str:
    """The provider-facing report as Markdown. Bad news before good news, throughout."""
    names = dict(gate_names or {})
    pct = lambda v: f"{v * 100:.0f}%"  # noqa: E731 — a formatter, not logic

    if score.verdict == "no_difference":
        headline = (
            f"no measurable difference — both arms {pct(score.after.fcc)}, "
            f"over {score.runs} runs with no variance"
        )
    elif score.verdict == "undetermined":
        headline = (
            f"undetermined — the change is smaller than this run's own variance "
            f"(±{score.noise_floor * 100:.0f} pts)"
        )
    else:
        headline = (
            f"{pct(score.before.fcc)} → {pct(score.after.fcc)} "
            f"({score.lift * 100:+.0f} pts)"
        )
    lines = [
        f"# {score.surface} — how an agent does on your surface",
        "",
        f"**First call correct: {headline}**",
        "",
        f"{score.tasks} intents × {score.runs} runs. Out-of-scope requests are scored "
        "separately, below, because the right answer there is to decline.",
        "",
    ]

    if score.broke:
        lines += ["## What got worse", ""]
        lines += [
            f"- **{i.goal}** — {pct(i.before)} → {pct(i.after)}" for i in score.broke
        ]
        lines.append("")

    if score.fixed:
        lines += ["## What got better", ""]
        lines += [
            f"- **{i.goal}** — {pct(i.before)} → {pct(i.after)}"
            for i in reversed(score.fixed)
        ]
        lines.append("")

    if score.still_failing:
        lines += [
            "## Still failing on both arms",
            "",
            "These are the ones to fix next; comprehension alone did not reach them.",
            "",
        ]
        lines += [f"- **{i.goal}**" for i in score.still_failing]
        lines.append("")

    moved = score.gates_moved
    if moved:
        lines += ["## Where the change came from", ""]
        lines += [
            f"- {names.get(gate, gate)}: {delta * 100:+.0f} pts"
            for gate, delta in moved
        ]
        lines.append("")

    b_ok, b_n = score.declined_before
    a_ok, a_n = score.declined_after
    if b_n:
        lines += [
            "## Out-of-scope requests correctly declined",
            "",
            f"- before: {b_ok}/{b_n}",
            f"- after: {a_ok}/{a_n}",
            "",
        ]

    lines += [
        "## Read this before quoting the number",
        "",
        *(f"- {c}" for c in score.caveats),
        "",
        f"Retrieval ceiling — the share of intents whose call the agent was even shown: "
        f"{pct(score.before.retrieval_ceiling)} before, {pct(score.after.retrieval_ceiling)} "
        "after. The agent cannot pick a call it never saw, so this bounds everything above.",
        "",
        f"Invented a tool that was never offered: {pct(score.before.hallucination)} before, "
        f"{pct(score.after.hallucination)} after.",
    ]
    return "\n".join(lines)
