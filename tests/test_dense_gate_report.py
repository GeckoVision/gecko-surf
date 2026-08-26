"""`scripts/dense_gate.py`'s report formatting, offline.

The script itself needs Atlas autoEmbed, so nothing ever executed its formatting on CI —
which is how it kept reading the retrieval card by a key name that meant the fallback.
These run its pure helpers over a card built from a stub retriever: no Mongo, no network.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from gecko.evaluate import GoldenTask, evaluate_golden

_SPEC = importlib.util.spec_from_file_location(
    "dense_gate",
    Path(__file__).resolve().parent.parent / "scripts" / "dense_gate.py",
)
assert _SPEC and _SPEC.loader
dense_gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dense_gate)


@dataclass(frozen=True)
class _Hit:
    name: str
    is_fallback: bool


class _Stub:
    def __init__(self, hits: dict[str, list[_Hit]]) -> None:
        self._hits = hits

    def search_scored(self, query: str, limit: int) -> list[_Hit]:
        return self._hits.get(query, [])[:limit]

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": "gold"}, {"name": "other"}]


_TASKS = [
    GoldenTask(goal="found", expect_ops=("gold",), archetype="keyword_echo"),
    GoldenTask(
        goal="fell-back", expect_ops=("gold",), archetype="paraphrase_no_overlap"
    ),
    GoldenTask(goal="off-topic", expect_ops=(), archetype="out_of_scope"),
]
_HITS = {
    "found": [_Hit("other", False), _Hit("gold", False)],
    "fell-back": [_Hit("gold", True)],
    "off-topic": [_Hit("other", True)],
}


def _card():
    return evaluate_golden(_Stub(_HITS), _TASKS, limit=30)


def test_paired_bootstrap_input_is_the_rankers_rank() -> None:
    # The fallback task contributes 0.0, not 1.0: the CI that decides the dense arm must not
    # credit an arm for a candidate nothing matched.
    rr = dense_gate._rr_by_task(_card())
    assert rr == {"found": 0.5, "fell-back": 0.0}


def test_archetype_recall5_counts_genuine_hits_only() -> None:
    assert dense_gate._archetype_recall5(_card()) == {
        "keyword_echo": (1, 1),
        "paraphrase_no_overlap": (0, 1),
    }


def test_the_per_task_table_marks_a_fallback_position_as_such() -> None:
    stub = _Stub(_HITS)
    card = _card()
    block = dense_gate._fixture_block("stub", stub, stub, card, card, card)
    assert "| found | keyword_echo | 2 | 2 | 2 |" in block
    assert "| fell-back | paraphrase_no_overlap | fb@1 | fb@1 | fb@1 |" in block
    assert "OOS✓" in block
    # Both readings reach the reader, labeled, for every arm.
    assert block.count("ranker") >= 3 and "with-fallback" in block
