from pathlib import Path

from gecko.catalog import Catalog, CatalogEntry
from gecko.client import AgentApiClient
from gecko.ingest import Operation, extract_operations, load_spec
from gecko.tools import to_tool

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _catalog() -> Catalog:
    return Catalog(extract_operations(load_spec(str(FIXTURE))))


def test_search_live_odds_finds_odds_endpoint():
    res = _catalog().search("live odds for a fixture")
    assert res, "expected results for a clear intent"
    assert "Odds" in res[0].operation.tags
    assert "odds" in res[0].operation.path


def test_search_scores_in_top_results():
    res = _catalog().search("match score updates")
    assert any("Scores" in e.operation.tags for e in res[:3])


def test_by_tag_covers_all_operations():
    grouped = _catalog().by_tag()
    assert {"Authentication", "Fixtures", "Odds", "Scores"} <= set(grouped)
    assert sum(len(v) for v in grouped.values()) == 18


def test_describe_renders_capability_map():
    text = _catalog().describe()
    assert "/api/odds/" in text
    assert "## Odds" in text


def test_empty_query_returns_nothing():
    assert _catalog().search("") == []


# FIX (0/97 discovery bug) — a MEANINGFUL query that shares no surface token with any
# operation used to score 0 across the board and be dropped by the `score > 0` filter,
# so search returned []: the op went invisible to the agent (the shipped "0/97" bug).
# search must NEVER return empty for a query that carries intent — it falls back to a
# non-semantic prior instead. (An empty/no-token query is a different case: no intent,
# still [] — guarded by test_empty_query_returns_nothing above.)
def test_search_never_empty_for_meaningful_zero_overlap_query():
    # "upcoming matches lineup" shares no token with any TxODDS operation's haystack.
    hits = _catalog().search("upcoming matches lineup")
    assert hits, "meaningful zero-overlap query must fall back, never return []"


def test_search_scored_marks_fallback_below_floor():
    # The fallback candidates are score-0 / is_fallback=True so an out-of-scope caller
    # can tell a real match (score>0) from a manufactured one (the confidence floor).
    scored = _catalog().search_scored("upcoming matches lineup")
    assert scored, "fallback must be non-empty"
    assert all(s.is_fallback and s.score == 0 for s in scored)
    # A genuine lexical hit is NOT flagged as fallback.
    real = _catalog().search_scored("live odds for a fixture")
    assert real and not real[0].is_fallback and real[0].score > 0


# FIX 1 — single source of truth for the tool name. When an op has no operationId,
# ingest synthesizes "post_/api/v1/charge"; to_tool sanitizes it but the catalog used
# to return the RAW id, so client.search (which filters on sanitized names) dropped
# every result. tool_name must agree across both layers.
def test_catalog_tool_name_matches_to_tool_for_synthesized_id():
    op = Operation(
        method="POST",
        path="/api/v1/charge",
        operation_id="post_/api/v1/charge",  # what ingest synthesizes (no operationId)
        summary="Create a new charge",
        description="",
        tags=[],
        parameters=[],
        request_body=None,
        responses={},
    )
    assert CatalogEntry(op).tool_name == to_tool(op)["name"]


SPEC_NO_OPID = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://api.example.test"}],
    "paths": {
        "/api/v1/charge": {
            "post": {
                "summary": "Create a new charge",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}


def test_search_finds_operation_without_operation_id():
    client = AgentApiClient(SPEC_NO_OPID)
    hits = client.search("create charge")
    assert hits, "search must return a hit for an op that has no operationId"
