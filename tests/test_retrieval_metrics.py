"""The SSOT must reproduce all three existing readings EXACTLY before anyone adopts it.

An equivalence test, not a behaviour test: if this module and the three live
implementations ever disagree on the same ranks, one of them is silently changing a
published number. The point is to make adoption provably boring.
"""

from __future__ import annotations

import pytest

from gecko.evaluate import _recall_mrr
from gecko.retrieval_metrics import RetrievalScore, mrr, recall_at, score

RANKS: list[list[int | None]] = [
    [],
    [1],
    [None],
    [1, 2, 3, None, None],
    [3, None, 1, 10, 4, None, 2],
    [None, None, None],
]


@pytest.mark.parametrize("ranks", RANKS)
def test_matches_evaluate_recall_mrr(ranks: list[int | None]) -> None:
    """`evaluate._recall_mrr` is the all_positive reading — misses in the denominator."""
    legacy = _recall_mrr(ranks)
    fresh = score(ranks, population="all_positive", ks=tuple(legacy["recall_at"]))
    for k, expected in legacy["recall_at"].items():
        assert fresh.recall_at[k] == pytest.approx(expected), f"recall@{k} drifted"
    assert fresh.mrr == pytest.approx(legacy["mrr"])


def test_scoreable_only_is_the_ranker_reading() -> None:
    """`retrieval_eval` scores only rows whose gold is reachable — the caller filters
    first, so the SSOT sees a shorter list and says so in the denominator."""
    all_rows: list[int | None] = [1, None, 3, None]
    scoreable = [r for r in all_rows if r is not None]
    lenient = score(scoreable, population="scoreable_only", ks=(1, 3))
    honest = score(all_rows, population="all_positive", ks=(1, 3))
    assert lenient.recall_at[3] == 1.0
    assert honest.recall_at[3] == 0.5
    # The same ranks, two legitimate questions, two very different numbers. Neither is
    # wrong; quoting either without its population is.
    assert lenient.means != honest.means


def test_a_miss_is_never_dropped() -> None:
    assert recall_at([None, None], 3) == 0.0
    assert mrr([None, 1]) == pytest.approx(0.5)
    assert score([None], population="all_positive").n == 1


def test_population_is_required() -> None:
    with pytest.raises(TypeError):
        score([1])  # type: ignore[call-arg]


def test_the_line_carries_its_denominator() -> None:
    line = score([1, None], population="all_positive", ks=(3,)).line(3)
    assert "n=2" in line and "misses included" in line


def test_empty_is_zero_not_a_crash() -> None:
    empty = score([], population="scoreable_only")
    assert empty.n == 0 and empty.mrr == 0.0 and empty.recall_at[3] == 0.0


def test_frozen() -> None:
    s = score([1], population="all_positive")
    assert isinstance(s, RetrievalScore)
    with pytest.raises(Exception):
        s.mrr = 1.0  # type: ignore[misc]
