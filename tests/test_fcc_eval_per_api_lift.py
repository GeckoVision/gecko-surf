"""``per_api_lift`` — the only honest before/after pair we can publish.

Two things are pinned here, and the distinction is the whole point:

1. **Arithmetic + refusals** against a SMALL COMMITTED GOLDEN SET (below, inline and
   reviewable). Synthetic on purpose: its numbers are chosen to be un-confusable with any
   published figure, so nobody can screenshot a test constant and call it a result.
2. **The published pair**, against the pinned benchmark run. That run's records live in
   ``private/`` (CLAUDE.md: numbers stay out of the public tree), so the test SKIPS when
   the file is absent and runs on a machine that has it. Point it anywhere with
   ``GECKO_FCC_PINNED_RUN=/path/to/run.jsonl``.

Every assertion below reads the RETURN VALUE of ``per_api_lift``. That is deliberate:
the repo's standing FEEDBACK is that published numbers had drifted from the sentences
around them, so the sentence itself is generated from the same object the test asserts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gecko.fcc_eval import (
    MIN_ATTEMPTS_PER_ARM,
    RunRecord,
    lift,
    per_api_lift,
)

# --------------------------------------------------------------------------- #
# The small committed golden set
# --------------------------------------------------------------------------- #
# One row per (api, arm, task, run). Rates are chosen to be recognisably synthetic:
#   painful_api : raw 2/10 = 0.20 -> gecko 9/10 = 0.90   (lift +0.70)
#   clean_api   : raw 10/10 = 1.00 -> gecko 10/10 = 1.00 (lift  0.00 — a MEASURED zero)
#   thin_api    : 4 attempts per arm  -> under the floor  -> could-not-answer
#   unpaired_api: 10 per arm, but the arms did not run the same tasks -> could-not-answer


def _rec(
    *,
    fixture: str,
    arm: str,
    goal: str,
    run: int,
    fcc: bool,
    archetype: str = "keyword_echo",
) -> RunRecord:
    """A RunRecord carrying only what ``per_api_lift`` reads (booleans + names)."""
    return RunRecord(
        fixture=fixture,
        archetype=archetype,
        goal=goal,
        arm=arm,
        run=run,
        picked=None,
        retrieval_hit=True,
        tool_correct=fcc,
        well_formed=fcc,
        args_match=fcc,
        fcc=fcc,
    )


def _arm_rows(
    fixture: str, arm: str, goals: list[str], runs: int, hits: int
) -> list[RunRecord]:
    """``runs`` runs over ``goals``; the first ``hits`` attempts (in order) are correct."""
    rows: list[RunRecord] = []
    for run in range(runs):
        for goal in goals:
            rows.append(
                _rec(
                    fixture=fixture,
                    arm=arm,
                    goal=goal,
                    run=run,
                    fcc=len([r for r in rows if r.fcc]) < hits,
                )
            )
    return rows


FIVE = [f"task-{i}" for i in range(5)]
TWO = [f"task-{i}" for i in range(2)]


def golden_records() -> list[RunRecord]:
    rows: list[RunRecord] = []
    # painful_api — 5 tasks x 2 runs = 10 attempts per arm
    rows += _arm_rows("painful_api", "raw", FIVE, runs=2, hits=2)
    rows += _arm_rows("painful_api", "gecko", FIVE, runs=2, hits=9)
    # out-of-scope rows must NOT reach the denominator: 4 more rows per arm, all correct
    # declines. If they were counted, painful_api's raw rate would move 0.20 -> 0.43.
    for arm in ("raw", "gecko"):
        for run in range(2):
            for goal in ("oos-a", "oos-b"):
                rows.append(
                    _rec(
                        fixture="painful_api",
                        arm=arm,
                        goal=goal,
                        run=run,
                        fcc=True,
                        archetype="out_of_scope",
                    )
                )
    # clean_api — both arms perfect: lift is a measured 0.0, not silence
    rows += _arm_rows("clean_api", "raw", FIVE, runs=2, hits=10)
    rows += _arm_rows("clean_api", "gecko", FIVE, runs=2, hits=10)
    # thin_api — 2 tasks x 2 runs = 4 attempts per arm, under the floor
    rows += _arm_rows("thin_api", "raw", TWO, runs=2, hits=0)
    rows += _arm_rows("thin_api", "gecko", TWO, runs=2, hits=4)
    # unpaired_api — same attempt count, different task sets
    rows += _arm_rows("unpaired_api", "raw", FIVE, runs=2, hits=1)
    rows += _arm_rows(
        "unpaired_api",
        "gecko",
        ["task-0", "task-1", "task-2", "task-3", "task-9"],
        2,
        9,
    )
    return rows


# --------------------------------------------------------------------------- #
# The pair
# --------------------------------------------------------------------------- #


def test_returns_the_before_after_pair_per_api_with_both_arms_named():
    block = per_api_lift(golden_records())

    painful = block.get("painful_api")
    assert painful is not None
    assert painful.baseline.arm == "raw"
    assert painful.treatment.arm == "gecko"
    assert painful.baseline.attempts == 10
    assert painful.treatment.attempts == 10
    assert painful.baseline.rate == 0.2
    assert painful.treatment.rate == 0.9
    assert block.lift_for("painful_api") == 0.7
    assert block.baseline_arm == "raw"
    assert block.treatment_arm == "gecko"


def test_out_of_scope_rows_are_outside_the_denominator():
    painful = per_api_lift(golden_records()).get("painful_api")
    assert painful is not None
    # 10 positive attempts per arm, not 14: the declines are scored elsewhere.
    assert painful.baseline.attempts == 10
    assert painful.baseline.first_call_correct == 2
    assert painful.tasks == 5


def test_a_measured_zero_lift_is_a_number_not_silence():
    block = per_api_lift(golden_records())
    assert block.lift_for("clean_api") == 0.0
    assert block.refusal_for("clean_api") is None
    clean = block.get("clean_api")
    assert clean is not None and clean.baseline.rate == 1.0


# --------------------------------------------------------------------------- #
# Refuse to score — "could not answer" is a distinct value, never 0.0
# --------------------------------------------------------------------------- #


def test_thin_denominator_refuses_to_score_and_is_not_zero():
    block = per_api_lift(golden_records())
    assert block.get("thin_api") is None
    assert block.lift_for("thin_api") is None  # distinct from 0.0
    assert block.lift_for("thin_api") is not block.lift_for("clean_api")
    assert block.refusal_for("thin_api") == "below_floor"
    row = next(u for u in block.unscored if u.api == "thin_api")
    assert row.baseline_attempts == 4 and row.treatment_attempts == 4
    assert row.minimum_attempts_per_arm == MIN_ATTEMPTS_PER_ARM


def test_unpaired_arms_refuse_to_score_even_above_the_floor():
    block = per_api_lift(golden_records())
    assert block.lift_for("unpaired_api") is None
    assert block.refusal_for("unpaired_api") == "unpaired"
    row = next(u for u in block.unscored if u.api == "unpaired_api")
    assert row.baseline_attempts == 10 and row.treatment_attempts == 10


def test_a_missing_arm_is_named_absent_not_scored_as_zero():
    rows = [
        r
        for r in golden_records()
        if not (r.fixture == "clean_api" and r.arm == "gecko")
    ]
    block = per_api_lift(rows)
    assert block.lift_for("clean_api") is None
    assert block.refusal_for("clean_api") == "arm_absent"


def test_an_unknown_api_is_could_not_answer_not_zero():
    block = per_api_lift(golden_records())
    assert block.lift_for("never_evaluated") is None
    assert block.refusal_for("never_evaluated") is None
    assert block.get("never_evaluated") is None


# --------------------------------------------------------------------------- #
# The reporting convention (shared with gecko.telemetry's per-surface block)
# --------------------------------------------------------------------------- #


def test_every_block_carries_provenance_denominator_and_producing_function():
    block = per_api_lift(golden_records())
    assert block.provenance == "benchmark"
    assert block.produced_by == "gecko.fcc_eval.per_api_lift"
    assert block.attempts == sum(
        e.baseline.attempts + e.treatment.attempts for e in block.entries
    )
    for entry in block.entries:
        assert entry.provenance == "benchmark"
        assert entry.produced_by == "gecko.fcc_eval.per_api_lift"
        assert entry.baseline.attempts > 0 and entry.treatment.attempts > 0
    for row in block.unscored:
        assert row.provenance == "benchmark"
        assert row.produced_by == "gecko.fcc_eval.per_api_lift"


def test_entries_are_ordered_by_denominator_then_name():
    block = per_api_lift(golden_records())
    keys = [
        (-(e.baseline.attempts + e.treatment.attempts), e.api) for e in block.entries
    ]
    assert keys == sorted(keys)


def test_the_published_sentence_is_generated_from_the_returned_numbers():
    painful = per_api_lift(golden_records()).get("painful_api")
    assert painful is not None
    sentence = painful.sentence()
    assert "painful_api" in sentence
    assert "20% -> 90%" in sentence
    assert "+70 pts" in sentence
    assert "raw" in sentence and "gecko" in sentence
    assert "10 attempts per arm" in sentence
    assert "benchmark" in sentence
    assert "gecko.fcc_eval.per_api_lift" in sentence


def test_arms_are_selectable_so_the_corpus_arm_reuses_one_definition():
    rows = golden_records()
    rows += _arm_rows("painful_api", "gecko_corpus", FIVE, runs=2, hits=10)
    block = per_api_lift(rows, baseline_arm="gecko", treatment_arm="gecko_corpus")
    assert block.baseline_arm == "gecko"
    assert block.treatment_arm == "gecko_corpus"
    assert block.lift_for("painful_api") == pytest.approx(0.1)


def test_pooled_lift_hides_the_per_api_structure_this_function_exposes():
    """Why this function exists: one pooled number averages two unlike APIs."""
    rows = [r for r in golden_records() if r.fixture in {"painful_api", "clean_api"}]
    pooled = lift(rows)
    block = per_api_lift(rows)
    assert pooled == pytest.approx(0.35)  # neither API's lift
    assert block.lift_for("painful_api") == 0.7
    assert block.lift_for("clean_api") == 0.0


# --------------------------------------------------------------------------- #
# The published pair — pinned benchmark run (skipped where the run is absent)
# --------------------------------------------------------------------------- #

_DEFAULT_PINNED = (
    Path(__file__).resolve().parents[1] / "private" / "2026-07-02-fcc-eval.jsonl"
)
PINNED = Path(os.environ.get("GECKO_FCC_PINNED_RUN", _DEFAULT_PINNED))


def _load(path: Path) -> list[RunRecord]:
    rows: list[RunRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            rows.append(
                _rec(
                    fixture=raw["fixture"],
                    arm=raw["arm"],
                    goal=raw["goal"],
                    run=raw["run"],
                    fcc=bool(raw["fcc"]),
                    archetype=raw["archetype"],
                )
            )
    return rows


@pytest.mark.skipif(
    not PINNED.exists(),
    reason=f"pinned benchmark run not present at {PINNED} (set GECKO_FCC_PINNED_RUN)",
)
def test_pinned_run_reproduces_the_published_pair_as_a_return_value():
    """The figures hand-copied into docs, re-derived as this function's return value."""
    block = per_api_lift(_load(PINNED))

    txodds = block.get("txodds")
    assert txodds is not None
    assert txodds.baseline.rate == 0.1
    assert txodds.treatment.rate == 0.65
    assert block.lift_for("txodds") == 0.55
    assert txodds.baseline.attempts == 120 and txodds.treatment.attempts == 120

    pegana = block.get("pegana")
    assert pegana is not None
    assert pegana.baseline.rate == 1.0
    assert pegana.treatment.rate == 1.0
    assert block.lift_for("pegana") == 0.0
