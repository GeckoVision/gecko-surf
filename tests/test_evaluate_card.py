"""The retrieval card must never let a FALLBACK position be read as a ranker hit.

``search_scored`` appends a never-empty fallback candidate (score 0, ``is_fallback``) when
nothing genuinely matched. Counting those positions as hits produced two false readings
that shipped:

  * headline recall that is FLAT across k (0.58 @1 = @3 = @5 = @20 on txodds) — the
    signature of "the target is at rank 1 or it was manufactured", quoted as if the ranker
    had found things at depth;
  * a FAKE REGRESSION — adding blurbs took txodds recall@20 from 1.00 to 0.92, because one
    op finally cleared the intent gate, the fallback stopped firing, and the gold op left
    the list.

So the card reports BOTH numbers under names that say which is which, and REFUSES the old
ambiguous keys rather than answering them with one of the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from gecko.evaluate import (
    AmbiguousMetric,
    GoldenTask,
    evaluate_golden,
    recall_summary,
)


@dataclass(frozen=True)
class _Hit:
    name: str
    is_fallback: bool


class _Fixed:
    """A retriever that replays a canned hit list per goal — the two arms below differ ONLY
    in whether the gold op arrives as a genuine hit or as a fallback, which is precisely the
    distinction under test."""

    def __init__(self, hits: dict[str, list[_Hit]]) -> None:
        self._hits = hits

    def search_scored(self, query: str, limit: int) -> list[_Hit]:
        return self._hits[query][:limit]


def _task(goal: str, *expect: str, archetype: str = "keyword_echo") -> GoldenTask:
    return GoldenTask(goal=goal, expect_ops=tuple(expect), archetype=archetype)


def _card() -> dict[str, Any]:
    tasks = [_task("genuine", "op_a"), _task("only-fallback", "op_b")]
    client = _Fixed(
        {
            "genuine": [_Hit("op_a", False)],
            "only-fallback": [_Hit("op_b", True)],
        }
    )
    return evaluate_golden(client, tasks, limit=30)


def test_a_fallback_position_is_not_credited_to_the_ranker() -> None:
    card = _card()
    assert card["ranker"]["recall_at"][1] == 0.5, "only the genuine hit is the ranker's"
    assert card["with_fallback"]["recall_at"][1] == 1.0
    assert card["n_via_fallback"] == 1
    assert card["n_positive"] == 2


def test_the_two_readings_are_split_per_task_too() -> None:
    row = next(r for r in _card()["per_task"] if r["goal"] == "only-fallback")
    assert row["rank_ranker"] is None
    assert row["rank_with_fallback"] == 1
    assert row["via_fallback"] is True
    assert row["hit_ranker"] is False
    assert row["hit_with_fallback"] is True


@pytest.mark.parametrize("retired", ["after_fix", "before_fix"])
def test_the_ambiguous_key_refuses_rather_than_picking_one(retired: str) -> None:
    card = _card()
    with pytest.raises(AmbiguousMetric) as exc:
        card[retired]
    assert "ranker" in str(exc.value) and "with_fallback" in str(exc.value)
    with pytest.raises(AmbiguousMetric):
        card["oos_pass_rate"][retired]


@pytest.mark.parametrize("retired", ["rank", "hit"])
def test_the_ambiguous_per_task_key_refuses_too(retired: str) -> None:
    row = _card()["per_task"][0]
    with pytest.raises(AmbiguousMetric):
        row[retired]


def test_the_fake_regression_only_appears_in_the_fallback_reading() -> None:
    """The txodds blurb episode in miniature, and the reason both numbers are reported.

    Control: the gold op is fallback-only at rank 1. Candidate: the ranker genuinely finds
    it, at rank 3. The candidate is strictly better, and the fallback reading calls it a
    regression from 1.00 to 0.00 at recall@1.
    """
    tasks = [_task("q", "gold")]
    control = evaluate_golden(_Fixed({"q": [_Hit("gold", True)]}), tasks, limit=30)
    candidate = evaluate_golden(
        _Fixed({"q": [_Hit("x", False), _Hit("y", False), _Hit("gold", False)]}),
        tasks,
        limit=30,
    )
    # The arms CAN diverge, and they diverge in opposite directions by reading.
    assert control["with_fallback"]["recall_at"][1] == 1.0
    assert candidate["with_fallback"]["recall_at"][1] == 0.0  # the manufactured "loss"
    assert control["ranker"]["recall_at"][3] == 0.0
    assert candidate["ranker"]["recall_at"][3] == 1.0  # the real gain


def test_the_summary_line_always_carries_both_labeled() -> None:
    text = recall_summary(_card())
    assert "ranker" in text and "fallback" in text
    assert "0.50" in text and "1.00" in text
