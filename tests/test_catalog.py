from pathlib import Path

from gecko.catalog import Catalog
from gecko.ingest import extract_operations, load_spec

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
