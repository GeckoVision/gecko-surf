"""The rankable unit: what it scores, what it gates, and what it refuses to be.

The equivalence with the shipped ranker is pinned elsewhere and more strongly than
any assertion here could: `tests/test_golden_set.py`, `tests/test_catalog*.py` and
`scripts/retrieval_report.py --check` all run the same `Catalog.search_scored`,
which now ranks through this module. These tests pin the PROPERTIES that make a
second corpus safe to point at it.
"""

from __future__ import annotations

import dataclasses

from gecko.catalog import Catalog, CatalogEntry
from gecko.ingest import Operation
from gecko.rankable import RankableUnit, fold_unit, rank_units, unit_text


def _tok(text: str) -> set[str]:
    return set(text.lower().split())


def _unit(**kwargs: object) -> RankableUnit:
    base: dict[str, object] = {"unit_id": "u", "title": ""}
    base.update(kwargs)
    return RankableUnit(**base)  # type: ignore[arg-type]


def _folded(*units: RankableUnit) -> list:
    return [fold_unit(u, _tok) for u in units]


def test_title_and_identity_score_twice_and_body_scores_once() -> None:
    # `alpha` is in the title (haystack + title), `beta` in the body (haystack only),
    # `gamma` in the identity (haystack + identity).
    unit = _unit(title="alpha", body="beta", identity="gamma")
    folded = fold_unit(unit, _tok)
    from gecko.rankable import score_folded

    assert score_folded(folded, {"alpha"}) == 2
    assert score_folded(folded, {"beta"}) == 1
    assert score_folded(folded, {"gamma"}) == 2


def test_unit_text_glues_every_ranked_field() -> None:
    unit = _unit(
        title="t", body="b", locator="l", identity="i", tags=("x",), aux_rank="a"
    )
    assert unit_text(unit).split() == ["t", "b", "l", "x", "i", "a"]
    # aux_intent is NOT in the ranked haystack: it gates, and it is already carried
    # into the haystack through aux_rank when the enricher wants it ranked.
    assert "secret" not in unit_text(_unit(title="t", aux_intent="secret"))


def test_gate_drops_a_body_only_match_and_ungated_keeps_it() -> None:
    """The gate is the whole difference between an API surface and a document one.

    A match won only inside reference prose is ranked either way; the gate decides
    whether it may certify the query as in-scope.
    """
    units = _folded(_unit(unit_id="prose", title="unrelated", body="depeg"))
    assert rank_units(units, {"depeg"}, gate=True, fallback=False) == []
    ungated = rank_units(units, {"depeg"}, gate=False, fallback=False)
    assert [r.index for r in ungated] == [0]
    assert ungated[0].score == 1


def test_no_fallback_means_an_empty_answer_is_possible() -> None:
    """A document corpus must be able to say 'not in these pages'. The never-empty
    0/97 prior is an API-surface policy and is not inherited."""
    units = _folded(_unit(unit_id="a", title="alpha"), _unit(unit_id="b", title="beta"))
    assert rank_units(units, {"nothing"}, gate=False, fallback=False) == []

    flagged = rank_units(units, {"nothing"}, gate=False, fallback=True)
    assert [r.is_fallback for r in flagged] == [True, True]
    assert [r.score for r in flagged] == [0, 0]


def test_fallback_order_is_by_rank_then_locator() -> None:
    units = _folded(
        _unit(unit_id="post", title="x", locator="/a", fallback_rank=1),
        _unit(unit_id="get-z", title="x", locator="/z", fallback_rank=0),
        _unit(unit_id="get-a", title="x", locator="/aa", fallback_rank=0),
    )
    ranked = rank_units(units, {"nothing"}, gate=False, fallback=True)
    assert [units[r.index].unit.unit_id for r in ranked] == ["get-a", "get-z", "post"]


def test_ties_break_on_locator_so_ranking_is_deterministic() -> None:
    units = _folded(
        _unit(unit_id="second", title="alpha", locator="/b"),
        _unit(unit_id="first", title="alpha", locator="/a"),
    )
    ranked = rank_units(units, {"alpha"}, gate=False, fallback=False)
    assert [units[r.index].unit.unit_id for r in ranked] == ["first", "second"]


def test_a_unit_carries_nothing_that_could_make_it_callable() -> None:
    """RANKABLE IS NOT CALLABLE. The reason a course page does not need a fabricated
    `Operation` is that the scorer never read the fields that make one callable — so
    the unit has nowhere to put a method, a parameter list or a request body, and a
    document projected into it cannot acquire them later."""
    fields = {f.name for f in dataclasses.fields(RankableUnit)}
    assert fields.isdisjoint(
        {"method", "parameters", "request_body", "responses", "operation", "server"}
    )


def test_an_operation_projects_into_a_unit_without_its_callable_half() -> None:
    op = Operation(
        method="POST",
        path="/v1/orders",
        operation_id="createOrder",
        summary="Place an order",
        description="Creates an order.",
        tags=["orders"],
        parameters=[],
        request_body=None,
        responses={},
    )
    unit = CatalogEntry(op).as_unit()
    assert unit.title == "Place an order"
    assert unit.identity == "createOrder"
    assert unit.locator == "/v1/orders"
    # A non-GET sorts after GETs in the never-empty prior, exactly as before.
    assert unit.fallback_rank == 1
    assert "POST" not in unit_text(unit)


def test_catalog_still_gates_and_still_never_returns_empty() -> None:
    """The two API-surface policies stay ON for operations — this is the regression
    guard for the delegation, in the one place a reader will look for it."""
    ops = [
        Operation(
            method="GET",
            path="/odds",
            operation_id="listOdds",
            summary="List odds",
            description="Odds for a fixture.",
            tags=[],
            parameters=[],
            request_body=None,
            responses={},
        )
    ]
    catalog = Catalog(ops)
    genuine = catalog.search_scored("list odds")
    assert [s.is_fallback for s in genuine] == [False]
    fallback = catalog.search_scored("chrysanthemum")
    assert [s.is_fallback for s in fallback] == [True]
