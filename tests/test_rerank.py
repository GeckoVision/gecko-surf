"""The rerank seam, and the two ceilings that decide whether to buy one.

The measurement these encode, run against Atlas on 2026-09-23 over txodds and
pegana, hybrid arm, paraphrase tasks:

    arm                          paraphrase retrieved@8    out-of-scope pass
    hybrid                              1.00                     0.00
    hybrid + ordering oracle            1.00                     0.00
    hybrid + refusing oracle            1.00                     1.00

Perfect ORDERING buys nothing. A second pass that can REFUSE buys everything.
That is why these two classes exist side by side, and why the protocol lets a
reranker return fewer names than it was given.
"""

from __future__ import annotations

import pytest

from gecko.rerank import (
    IdentityReranker,
    OracleReranker,
    Reranker,
    RefusingOracleReranker,
    reorder,
)


class _Hit:
    """The shape every arm's hit shares: a name, and whatever else it carries."""

    def __init__(self, name: str, score: float = 1.0):
        self.name = name
        self.score = score


POOL = ["get_odds", "get_fixture", "list_sports"]
GOLD = {"who is playing tonight": frozenset({"get_fixture"})}


def test_identity_is_the_control_arm() -> None:
    assert IdentityReranker().rerank("q", POOL, 3) == POOL
    assert IdentityReranker().rerank("q", POOL, 2) == POOL[:2]


def test_ordering_oracle_lifts_the_gold_and_keeps_the_rest() -> None:
    ranked = OracleReranker(GOLD).rerank("who is playing tonight", POOL, 3)
    assert ranked[0] == "get_fixture"
    assert set(ranked) == set(POOL), "ordering never drops a candidate"


def test_ordering_oracle_cannot_refuse() -> None:
    """The finding, as a test. Reordering an out-of-scope pool still answers it."""
    ranked = OracleReranker(GOLD).rerank("what is the weather", POOL, 3)
    assert ranked, "an ordering pass always returns something, which is the problem"


def test_refusing_oracle_declines_when_nothing_is_right() -> None:
    assert RefusingOracleReranker(GOLD).rerank("what is the weather", POOL, 3) == []
    ranked = RefusingOracleReranker(GOLD).rerank("who is playing tonight", POOL, 3)
    assert ranked[0] == "get_fixture"


def test_an_oracle_is_per_query_not_per_set() -> None:
    """A union-of-all-gold oracle reports a ceiling that does not exist.

    With union gold, an out-of-scope query still finds a name that is gold for
    some other task, so the refusing oracle never refuses and measures 0.00
    where the truth is 1.00. This is the bug that version had, caught by running
    it, and the reason both oracles take a per-query mapping.
    """
    union = {query: frozenset({"get_fixture"}) for query in ("a", "b")}
    assert RefusingOracleReranker(union).rerank("unseen query", POOL, 3) == []


def test_a_reranker_never_invents_a_candidate() -> None:
    """The one promise a caller may assume without checking."""
    for reranker in (
        IdentityReranker(),
        OracleReranker(GOLD),
        RefusingOracleReranker(GOLD),
    ):
        out = reranker.rerank("who is playing tonight", POOL, 5)
        assert set(out) <= set(POOL), f"{type(reranker).__name__} invented a name"


def test_reorder_keeps_the_hit_objects_intact() -> None:
    """The reranker speaks names; the caller keeps score and provenance."""
    hits = [_Hit("get_odds", 0.4), _Hit("get_fixture", 0.9)]
    out = reorder(hits, ["get_fixture", "get_odds"])
    assert [h.name for h in out] == ["get_fixture", "get_odds"]
    assert [h.score for h in out] == [0.9, 0.4]


def test_reorder_drops_what_the_reranker_dropped() -> None:
    hits = [_Hit("get_odds"), _Hit("get_fixture")]
    assert reorder(hits, []) == []
    assert [h.name for h in reorder(hits, ["get_fixture"])] == ["get_fixture"]


@pytest.mark.parametrize(
    "reranker",
    [IdentityReranker(), OracleReranker(GOLD), RefusingOracleReranker(GOLD)],
)
def test_every_implementation_satisfies_the_protocol(reranker: object) -> None:
    assert isinstance(reranker, Reranker)
