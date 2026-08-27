"""A tier eval with nothing to score must NOT read as a perfect pass.

``evaluate_tier`` returned ``precision 1.0, recall 1.0`` whenever a denominator was empty —
so an empty label join (a moved fixture, a renamed ``operation_id``, a spec that no longer
parses) cleared a gate whose spec says the tier signal does not ship below 0.95/0.80. The
one guard against that lived in a single test, so every other caller got the vacuous pass
silently.

``gecko.score`` already draws this distinction: a measured zero is a RESULT
(``no_difference``), an unmeasurable one is ``undetermined``. This is the same rule for the
tier gate — and an unmeasured gate never ships.
"""

from __future__ import annotations

from pathlib import Path

from gecko.evaluate import evaluate_tier, load_tier_labels
from gecko.ingest import extract_operations, load_spec

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
LABELS = GOLDEN / "tier_labels.jsonl"


def _privy_arm() -> tuple[list, dict[str, str]]:
    """The real, populated join — the arm that MUST still measure, so the empty one below
    is a contrast and not a broken harness."""
    rows = [r for r in load_tier_labels(LABELS) if r["spec"] == "privy"]
    by_id = {
        o.operation_id: o
        for o in extract_operations(load_spec(str(GOLDEN / "privy_openapi.json")))
    }
    labels = {r["operation_id"]: r["tier"] for r in rows}
    return [by_id[oid] for oid in labels if oid in by_id], labels


def test_an_empty_label_join_is_undetermined_not_a_perfect_score() -> None:
    result = evaluate_tier([], {})
    assert result.precision is None, "an empty denominator is not a measured 1.0"
    assert result.recall is None
    assert result.transfer_true == 0
    assert result.verdict == "undetermined"
    assert result.clears_ship_gate() is False, "a null must never clear the ship gate"
    assert (
        "no labeled" in result.undetermined_reason.lower() or result.undetermined_reason
    )


def test_the_populated_arm_still_measures_so_the_two_can_diverge() -> None:
    ops, labels = _privy_arm()
    result = evaluate_tier(ops, labels)
    assert result.transfer_true >= 5 and result.transfer_high_pred >= 5
    assert isinstance(result.precision, float) and isinstance(result.recall, float)
    assert result.verdict == "ships"
    assert result.clears_ship_gate() is True
    assert result.undetermined_reason == ""


def test_a_measured_zero_recall_is_a_determined_refusal_not_undetermined() -> None:
    """The score.py rule: measured badly is not the same as unmeasurable.

    A liveness GET labeled ``transfer`` is a recall miss the classifier genuinely makes (it
    calls it ``read``/high), so recall is 0.0 — a number — while precision has no
    high-confidence transfer prediction to score and stays ``None``. The gate is BLOCKED
    (the measured half already fails), not undetermined.
    """
    spec = load_spec(str(FIXTURES / "pegana_openapi.json"))
    op = next(o for o in extract_operations(spec) if o.operation_id == "live")
    result = evaluate_tier([op], {"live": "transfer"})
    assert result.recall == 0.0, "a miss the classifier really made is a measured zero"
    assert result.precision is None, "no high-confidence transfer prediction to score"
    assert result.verdict == "blocked"
    assert result.clears_ship_gate() is False
