"""The provider report — and specifically the four things it refuses to do.

`gecko.score` adds no measurement. Every number in it comes from `fcc_eval`, so the only
thing worth testing is the honesty: it must not pool unlike surfaces, must not call noise a
result, must report regressions, and must report what still fails. Each of those has a test
here that goes red if the refusal is removed, because a refusal nothing checks is a comment.
"""

from __future__ import annotations

import pytest

from gecko.fcc_eval import RunRecord
from gecko.score import ScoreError, render_report, score_surface


def record(
    *,
    goal: str,
    arm: str,
    run: int,
    fcc: bool,
    fixture: str = "letmebuy",
    archetype: str = "keyword_echo",
    retrieval: bool | None = None,
    hallucinated: bool = False,
) -> RunRecord:
    """One outcome. Gates default to agreeing with `fcc` so a test states only what it means."""
    hit = fcc if retrieval is None else retrieval
    return RunRecord(
        fixture=fixture,
        archetype=archetype,
        goal=goal,
        arm=arm,
        run=run,
        picked=goal if fcc else None,
        retrieval_hit=hit,
        tool_correct=fcc,
        well_formed=fcc,
        args_match=fcc,
        fcc=fcc,
        hallucinated=hallucinated,
    )


def steady(goal: str, *, before: bool, after: bool, runs: int = 3) -> list[RunRecord]:
    """The same outcome on every run — no variance, so a delta is unambiguous."""
    out: list[RunRecord] = []
    for run in range(runs):
        out.append(record(goal=goal, arm="raw", run=run, fcc=before))
        out.append(record(goal=goal, arm="gecko", run=run, fcc=after))
    return out


# ---------------------------------------------------------------------------
# refusal 1 — it will not pool unlike surfaces
# ---------------------------------------------------------------------------


def test_records_from_two_surfaces_refuse_to_become_one_report() -> None:
    """`fcc_eval.lift`'s own docstring is the reason: on a pinned run a pooled +0.30 was the
    mean of a +0.55 API and a +0.00 API, and described neither. A provider report is about
    ONE surface, so this raises instead of averaging."""
    records = steady("buy water", before=False, after=True)
    records += [
        record(goal="swap a token", arm=arm, run=run, fcc=True, fixture="raydium")
        for arm in ("raw", "gecko")
        for run in range(3)
    ]
    with pytest.raises(ScoreError) as excinfo:
        score_surface(records)
    message = str(excinfo.value)
    assert "letmebuy" in message and "raydium" in message
    assert "score each separately" in message


def test_arms_that_share_no_intent_refuse_rather_than_differencing_two_task_lists() -> (
    None
):
    """Both arms present, both measured, NOTHING in common — so the lift is a difference of
    two different questions.

    `_intents` drops an intent only one arm attempted (rightly: an asymmetric task list
    would flatter whichever arm ran the easy one), but the headline FCC is pooled over
    every record regardless. Drop every pairing and the report still printed a lift, with
    `tasks=0` as the only sign that nothing was actually compared.
    """
    records = [record(goal="buy water", arm="raw", run=r, fcc=False) for r in range(3)]
    records += [
        record(goal="swap a token", arm="gecko", run=r, fcc=True) for r in range(3)
    ]
    with pytest.raises(ScoreError) as excinfo:
        score_surface(records)
    message = str(excinfo.value)
    assert "paired_intents" in message
    assert "not a negative result" in message


def test_one_shared_intent_is_enough_to_report_so_the_refusal_is_a_guard() -> None:
    # The contrast arm: add the pairing back and the same records score normally.
    records = [record(goal="buy water", arm="raw", run=r, fcc=False) for r in range(3)]
    records += [
        record(goal="swap a token", arm="gecko", run=r, fcc=True) for r in range(3)
    ]
    records += steady("top up the tab", before=False, after=True)
    score = score_surface(records)
    assert score.tasks == 1


def test_a_missing_arm_refuses_rather_than_reporting_half_a_comparison() -> None:
    only_gecko = [
        record(goal="buy water", arm="gecko", run=r, fcc=True) for r in range(3)
    ]
    with pytest.raises(ScoreError) as excinfo:
        score_surface(only_gecko)
    assert "raw" in str(excinfo.value)


# ---------------------------------------------------------------------------
# refusal 2 — it will not call run-to-run noise a result
# ---------------------------------------------------------------------------


def test_a_lift_inside_the_arms_own_variance_is_reported_as_undetermined() -> None:
    """Two intents that flip between runs on BOTH arms produce movement with no signal.

    The headline must say so rather than print the midpoint: a number quoted out of a
    document loses its error bar, so the document must not offer one it cannot defend.
    """
    # `run_variance` reads each arm's PER-RUN aggregate, so the aggregate itself has to
    # move — two intents flipping in opposite directions leaves it flat at 0.5 and looks
    # deterministic. Here each arm sweeps 100% -> 50% -> 0% in opposite orders: identical
    # means, so the lift is zero, with half a point of movement per arm to hide it in.
    raw_by_run = {0: (True, True), 1: (True, False), 2: (False, False)}
    gecko_by_run = {0: (False, False), 1: (True, False), 2: (True, True)}
    records: list[RunRecord] = []
    for run in range(3):
        a_raw, b_raw = raw_by_run[run]
        a_gecko, b_gecko = gecko_by_run[run]
        records.append(record(goal="a", arm="raw", run=run, fcc=a_raw))
        records.append(record(goal="b", arm="raw", run=run, fcc=b_raw))
        records.append(record(goal="a", arm="gecko", run=run, fcc=a_gecko))
        records.append(record(goal="b", arm="gecko", run=run, fcc=b_gecko))

    score = score_surface(records)
    assert score.noise_floor > 0.0, "this fixture is meant to have per-run movement"
    assert score.lift_is_noise is True
    report = render_report(score)
    assert "undetermined" in report
    assert any("RUN-TO-RUN" in c for c in score.caveats)


def test_a_lift_larger_than_the_noise_floor_is_reported_as_a_number() -> None:
    """The other half of the gate: a guard that never passes is an outage, not a guard."""
    records = steady("a", before=False, after=True) + steady(
        "b", before=False, after=True
    )
    score = score_surface(records)
    assert score.noise_floor == 0.0
    assert score.lift == pytest.approx(1.0)
    assert score.lift_is_noise is False
    assert "0% → 100%" in render_report(score)


# ---------------------------------------------------------------------------
# refusal 3 — regressions are reported, and first
# ---------------------------------------------------------------------------


def test_an_intent_comprehension_broke_is_reported_before_the_wins() -> None:
    """A report that only lists improvements is marketing.

    If comprehension broke something the raw dump got right, that is the most useful line in
    the document, and it appears above "what got better".
    """
    records = steady("worked before", before=True, after=False)
    records += steady("fixed now", before=False, after=True)

    score = score_surface(records)
    assert [i.goal for i in score.broke] == ["worked before"]
    assert [i.goal for i in score.fixed] == ["fixed now"]

    report = render_report(score)
    assert report.index("What got worse") < report.index("What got better")


# ---------------------------------------------------------------------------
# refusal 4 — what still fails is reported too
# ---------------------------------------------------------------------------


def test_intents_failing_on_both_arms_are_named_as_the_work_remaining() -> None:
    records = steady("reachable", before=False, after=True)
    records += steady("nobody can call this", before=False, after=False)
    score = score_surface(records)
    assert [i.goal for i in score.still_failing] == ["nobody can call this"]
    assert "Still failing on both arms" in render_report(score)
    assert "nobody can call this" in render_report(score)


# ---------------------------------------------------------------------------
# attribution, ceilings, and the parts a provider acts on
# ---------------------------------------------------------------------------


def test_the_report_says_which_gate_moved() -> None:
    """A headline that moves because RETRIEVAL moved is a different claim from one that
    moves because args_match moved: could not find it, versus found it and filled it wrong."""
    records: list[RunRecord] = []
    for run in range(3):
        # raw never even surfaces the call; gecko surfaces it and gets it right.
        records.append(
            record(goal="buy water", arm="raw", run=run, fcc=False, retrieval=False)
        )
        records.append(
            record(goal="buy water", arm="gecko", run=run, fcc=True, retrieval=True)
        )
    score = score_surface(records)
    moved = dict(score.gates_moved)
    assert moved["retrieval"] == pytest.approx(1.0)
    assert "Where the change came from" in render_report(score)


def test_a_run_at_its_retrieval_ceiling_says_the_next_gain_is_not_generation() -> None:
    """Every call the agent was shown, it got right — so tuning generation cannot help."""
    records = steady("buy water", before=False, after=True)
    score = score_surface(records)
    assert score.after.fcc == pytest.approx(score.after.retrieval_ceiling)
    assert any("retrieval ceiling" in c for c in score.caveats)


def test_out_of_scope_is_scored_apart_from_the_headline() -> None:
    """The right answer to an out-of-scope request is to decline. Folding a refusal into a
    success rate hides whether a surface knows its own edges."""
    records = steady("buy water", before=False, after=True)
    for run in range(3):
        for arm, ok in (("raw", False), ("gecko", True)):
            records.append(
                record(
                    goal="book me a flight",
                    arm=arm,
                    run=run,
                    fcc=ok,
                    archetype="out_of_scope",
                )
            )
    score = score_surface(records)
    assert score.declined_before == (0, 3)
    assert score.declined_after == (3, 3)
    # the headline counts positives only — one intent, not two
    assert score.tasks == 1
    assert score.after.fcc == pytest.approx(1.0)


def test_an_intent_only_one_arm_attempted_is_dropped_rather_than_flattering_it() -> (
    None
):
    """An asymmetric task list would otherwise let whichever arm ran an extra intent claim
    it. A before/after needs both sides or it is not a comparison."""
    records = steady("both arms ran this", before=False, after=True)
    records += [
        record(goal="only gecko ran this", arm="gecko", run=r, fcc=True)
        for r in range(3)
    ]
    score = score_surface(records)
    assert [i.goal for i in score.intents] == ["both arms ran this"]


def test_the_caveats_travel_in_the_object_not_only_the_prose() -> None:
    """A number gets copied out of a document; its qualifications do not. So they are a
    field, and every renderer has to carry them."""
    score = score_surface(steady("buy water", before=False, after=True))
    assert score.caveats
    assert any("COMPREHENSION lift" in c for c in score.caveats)
    assert any("not a strawman" in c for c in score.caveats)
    for caveat in score.caveats:
        assert caveat in render_report(score)


def test_one_run_says_it_cannot_separate_a_result_from_variance() -> None:
    score = score_surface(steady("buy water", before=False, after=True, runs=1))
    assert any("1 run(s)" in c for c in score.caveats)


def test_no_records_refuses() -> None:
    with pytest.raises(ScoreError):
        score_surface([])


# ---------------------------------------------------------------------------
# a determined zero is not an undetermined one — found by a real run, not by design
# ---------------------------------------------------------------------------


def test_both_arms_perfect_reports_no_difference_rather_than_undetermined() -> None:
    """Pegana scored 100% on BOTH arms across seven runs with zero variance.

    The first version of this module called that "undetermined — run more", because
    `abs(0.0) <= 0.0`. That told a provider to spend money re-measuring a settled answer.
    A lift of zero with a noise floor of zero is a RESULT: on these intents the raw
    specification already got every call right, so comprehension had nothing to add.
    """
    records = steady("a", before=True, after=True) + steady(
        "b", before=True, after=True
    )
    score = score_surface(records)
    assert score.noise_floor == 0.0 and score.lift == 0.0
    assert score.verdict == "no_difference"
    assert score.lift_is_noise is False, "a determined zero is not noise"

    report = render_report(score)
    assert "no measurable difference" in report
    assert "undetermined" not in report
    # and it says the thing worth checking before anyone celebrates
    assert any("too close to its own wording" in c for c in score.caveats)


def test_a_small_lift_against_noisy_arms_is_still_undetermined() -> None:
    """The other branch has to keep working, or the fix above just deleted the guard."""
    records: list[RunRecord] = []
    raw_by_run = {0: (True, True), 1: (True, False), 2: (False, False)}
    gecko_by_run = {0: (False, False), 1: (True, False), 2: (True, True)}
    for run in range(3):
        a_raw, b_raw = raw_by_run[run]
        a_gecko, b_gecko = gecko_by_run[run]
        records.append(record(goal="a", arm="raw", run=run, fcc=a_raw))
        records.append(record(goal="b", arm="raw", run=run, fcc=b_raw))
        records.append(record(goal="a", arm="gecko", run=run, fcc=a_gecko))
        records.append(record(goal="b", arm="gecko", run=run, fcc=b_gecko))
    score = score_surface(records)
    assert score.noise_floor > 0.0
    assert score.verdict == "undetermined"
    assert score.lift_is_noise is True
