"""The retrieval numbers, pinned where CI can see them move.

Until now the figure everyone quotes for this engine lived in a gitignored note,
produced by a loop that no longer exists. Nothing would have caught the ranker
regressing, and nothing could re-derive the number to check it.

These tests do two separate jobs, and the split matters:

* the committed scorecard still matches a fresh run -- so a ranker change shows
  up as a diff somebody has to look at, not as a silent drift;
* the specific figures in circulation are the ones we measure -- so quoting them
  outside the repo stays honest.

A full run over all four golden sets takes under four seconds, which is why this
is an ordinary test and not an opt-in marker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import retrieval_report  # noqa: E402

SCORECARD = ROOT / "docs" / "retrieval" / "scorecard.json"


@pytest.fixture(scope="module")
def fresh() -> dict[str, Any]:
    return retrieval_report.build()


@pytest.fixture(scope="module")
def committed() -> dict[str, Any]:
    assert SCORECARD.is_file(), "run: uv run python scripts/retrieval_report.py"
    return json.loads(SCORECARD.read_text(encoding="utf-8"))


def test_the_committed_scorecard_matches_a_fresh_run(
    committed: dict[str, Any], fresh: dict[str, Any]
) -> None:
    assert committed == json.loads(json.dumps(fresh)), (
        "the scorecard in docs/retrieval/ is stale. Either the ranker moved or a golden "
        "set did. Re-run `uv run python scripts/retrieval_report.py` and read the diff "
        "before committing it: that diff is the only record of a retrieval change."
    )


def test_the_paraphrase_figure_in_circulation_is_the_one_we_measure(
    fresh: dict[str, Any],
) -> None:
    """0.04 ranker / 0.22 with_fallback at k=8, over 27 pooled paraphrase tasks.

    This is the number quoted in CLAUDE.md and in `private/2026-09-20-jev-intent-routing.md`.
    It is pinned so that it cannot quietly stop being true while still being quoted.
    """
    paraphrase = fresh["pooled"]["overlap"]["by_archetype"]["paraphrase_no_overlap"]
    assert paraphrase["n"] == 27
    assert paraphrase["ranker"]["8"] == pytest.approx(0.04, abs=0.005)
    assert paraphrase["with_fallback"]["8"] == pytest.approx(0.22, abs=0.005)


def test_the_easy_archetypes_are_solved_and_stay_solved(fresh: dict[str, Any]) -> None:
    """Keyword echo is what overlap counting is guaranteed to win. If this falls, the
    regression is in the ranker itself, not in anything subtle about paraphrases."""
    pooled = fresh["pooled"]["overlap"]["by_archetype"]
    assert pooled["keyword_echo"]["ranker"]["8"] == pytest.approx(1.00, abs=0.005)
    assert pooled["near_dup_disambiguation"]["ranker"]["8"] >= 0.94


def test_the_two_readings_part_company_only_on_paraphrases(
    fresh: dict[str, Any],
) -> None:
    """The structural finding, as an assertion.

    Where the query shares words with the gold op, the ranker finds it and the
    never-empty fallback never fires, so both readings agree. Where it shares
    none, the ranker scores zero by arithmetic and the gap between the readings
    is entirely the fallback's position. That gap is the metric talking about
    itself, and it is why `0.04` must never be quoted as "retrieval is broken".
    """
    pooled = fresh["pooled"]["overlap"]["by_archetype"]
    for archetype in ("keyword_echo", "near_dup_disambiguation"):
        entry = pooled[archetype]
        assert entry["ranker"] == entry["with_fallback"], (
            f"{archetype}: the readings diverged, so the fallback is now firing on "
            "queries that do share words with their target. That is a ranker regression."
        )
    paraphrase = pooled["paraphrase_no_overlap"]
    assert paraphrase["with_fallback"]["8"] > paraphrase["ranker"]["8"]


def test_every_golden_set_is_in_the_report(fresh: dict[str, Any]) -> None:
    """Four sets, not the two the older runner knows about. The headline is pooled over
    all four, so a report missing one cannot reproduce it."""
    assert set(fresh["sets"]) == {"txodds", "pegana", "privy", "birdeye"}
    for name, entry in fresh["sets"].items():
        assert entry["arms"]["overlap"]["n_positive"] > 0, (
            f"{name} contributed no positive task"
        )


def test_bm25_is_measured_and_did_not_earn_selection(fresh: dict[str, Any]) -> None:
    """The arm that was built and never selected, and why it stays that way.

    This is pinned because "BM25 is better" is the obvious assumption, the plan
    made it, and the measurement refused it. If someone selects BM25F later, this
    test should be the thing that makes them show a number first.
    """
    overlap = fresh["pooled"]["overlap"]["by_archetype"]
    bm25 = fresh["pooled"]["bm25"]["by_archetype"]

    assert bm25["keyword_echo"]["ranker"]["8"] < overlap["keyword_echo"]["ranker"]["8"]
    assert (
        bm25["near_dup_disambiguation"]["ranker"]["1"]
        < (overlap["near_dup_disambiguation"]["ranker"]["1"])
    )
    assert bm25["paraphrase_no_overlap"]["ranker"]["8"] == 0.0, (
        "a lexical arm cannot score an archetype defined by sharing no tokens. If this "
        "ever passes, the golden set's zero-overlap rule broke, not retrieval."
    )


def test_both_arms_score_the_same_pool(fresh: dict[str, Any]) -> None:
    """Otherwise the comparison is between two different questions."""
    for name, entry in fresh["sets"].items():
        overlap, bm25 = entry["arms"]["overlap"], entry["arms"]["bm25"]
        assert overlap["n_positive"] == bm25["n_positive"], name
        assert overlap["n_oos"] == bm25["n_oos"], name


def test_the_third_reading_gives_a_lexical_arm_nothing(fresh: dict[str, Any]) -> None:
    """`retrieved` counts what either arm genuinely ranked. With no dense arm there is
    nothing extra to count, so it must equal `ranker` exactly.

    If these ever diverge on a lexical-only arm, the reading has started crediting the
    query-independent prior, which is the bug it exists to avoid.
    """
    for arm in fresh["arms"]:
        for archetype, entry in fresh["pooled"][arm]["by_archetype"].items():
            assert entry["retrieved"] == entry["ranker"], (
                f"{arm}/{archetype}: a lexical arm cannot retrieve more than it ranks"
            )
