"""Okapi BM25 (BM25F) retrieval arm — unit falsifiers for each lever.

BM25 is a SELECTABLE arm (``catalog.BM25Index``), not the live-path default: these tests
pin the four levers our overlap-count scorer lacks (IDF, TF-saturation, length-norm,
OpenAPI-remapped field weights) plus the preserved identifier tokenizer and the never-empty
/ OOS fallback contract. The scale gate (adopt only when usable_ops>50 AND recall@3<0.8)
is measured by ``scripts/retrieval_arms_eval.py``, not here.
"""

from __future__ import annotations

from gecko.catalog import BM25Index, Catalog
from gecko.ingest import Operation


def _op(
    *,
    method: str = "GET",
    path: str = "/x",
    operation_id: str = "op",
    summary: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> Operation:
    return Operation(
        method=method,
        path=path,
        operation_id=operation_id,
        summary=summary,
        description=description,
        tags=tags or [],
        parameters=[],
        request_body=None,
        responses={},
    )


def _index(ops: list[Operation]) -> BM25Index:
    return BM25Index(Catalog(ops).entries)


def _ranked_ids(index: BM25Index, query: str) -> list[str]:
    return [h.entry.operation.operation_id for h in index.search_scored(query, 10)]


def test_idf_downweights_ubiquitous_term() -> None:
    # "get" appears in every op (df=N -> IDF~0); "odds" appears in one (rare -> high IDF).
    # A query carrying both must rank the discriminating op first, not one that only echoes
    # the ubiquitous term. The overlap-count scorer (every match = 1) cannot do this.
    ops = [
        _op(operation_id="getScores", summary="get scores"),
        _op(operation_id="getFixtures", summary="get fixtures"),
        _op(operation_id="getOdds", summary="get odds"),
    ]
    ranked = _ranked_ids(_index(ops), "get odds")
    assert ranked[0] == "getOdds"


def test_field_weights_prefer_summary_over_operationid() -> None:
    # OpenAPI-remapped priors (chub's id=4 INVERTED): summary is intent-bearing (high weight),
    # the auto-generated operationId is low-signal (low weight). A summary match must outrank
    # an op that carries the same term ONLY in its junk operationId field.
    ops = [
        _op(operation_id="getThing", summary="manage odds", path="/thing"),
        _op(operation_id="getOddsHandler", summary="unrelated widget", path="/widget"),
    ]
    ranked = _ranked_ids(_index(ops), "odds")
    assert ranked[0] == "getThing"


def test_length_norm_prefers_concise_field() -> None:
    # Same field, same term: the concise op must beat the verbose one padded with filler —
    # length normalization (b=0.75) stops a long description from winning on token count alone.
    ops = [
        _op(operation_id="concise", description="odds"),
        _op(
            operation_id="verbose",
            description="odds " + " ".join(f"w{i}" for i in range(80)),
        ),
    ]
    ranked = _ranked_ids(_index(ops), "odds")
    assert ranked[0] == "concise"


def test_tf_saturation_is_sublinear() -> None:
    # A term repeated 10x must score MORE than once but FAR LESS than 10x — Robertson
    # saturation (k1=1.5), which a linear overlap/TF sum lacks.
    one = _index([_op(operation_id="one", summary="odds")])
    many = _index([_op(operation_id="many", summary=" ".join(["odds"] * 10))])
    s1 = one.search_scored("odds", 1)[0].score
    s10 = many.search_scored("odds", 1)[0].score
    assert s1 < s10 < 10 * s1


def test_identifier_tokenizer_matches_camelcase_operationid() -> None:
    # The identifier tokenizer is KEPT: a camelCase operationId splits so a bare intent token
    # matches it even when it is the sole signal (thin summary, path carries no such token).
    op = _op(
        operation_id="getApiOddsSnapshotFixtureid",
        summary="",
        path="/x/{fixtureid}",
    )
    hits = _index([op]).search_scored("odds", 5)
    assert hits and not hits[0].is_fallback and hits[0].score > 0
    assert hits[0].entry.operation.operation_id == "getApiOddsSnapshotFixtureid"


def test_never_empty_fallback_for_zero_overlap_query() -> None:
    # Same never-empty contract as the overlap scorer: a meaningful zero-overlap query falls
    # back to a query-independent prior (flagged score-0 / is_fallback), never [].
    ops = [_op(operation_id="getOdds", summary="get odds")]
    hits = _index(ops).search_scored("unrelated houseplant watering", 5)
    assert hits
    assert all(h.is_fallback and h.score == 0.0 for h in hits)


def test_empty_query_returns_nothing() -> None:
    assert _index([_op(summary="odds")]).search_scored("", 5) == []


def test_oos_top1_is_below_confidence_floor() -> None:
    # An out-of-scope intent with no corpus overlap must leave the top-1 flagged is_fallback
    # (the lexical-anchored confidence floor) so the OOS guard never reads a false positive.
    ops = [
        _op(operation_id="createWallet", summary="create wallet"),
        _op(operation_id="listUsers", summary="list users"),
    ]
    hits = _index(ops).search_scored("play relaxing jazz music", 5)
    assert hits[0].is_fallback


def test_default_catalog_search_is_unchanged_by_bm25() -> None:
    # BM25 is a separate arm: constructing an index must not alter the overlap-count default
    # (Catalog.search_scored) — the live path stays byte-identical until a measured win.
    ops = [_op(operation_id="getOdds", summary="get odds for a fixture")]
    cat = Catalog(ops)
    before = [s.score for s in cat.search_scored("odds", 5)]
    BM25Index(cat.entries)  # building the arm must have no side effect on the catalog
    after = [s.score for s in cat.search_scored("odds", 5)]
    assert before == after and before and isinstance(before[0], int)
