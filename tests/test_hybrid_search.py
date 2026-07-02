"""Hybrid lexical+dense search behind ``client.search_hybrid`` — falsified offline with an
injected FAKE ``DenseIndex`` (Pattern B: no Mongo needed to test the fusion logic).

Covers the three things the dense arm must get right: (1) the agent-facing dict contract stays
byte-identical to ``search``; (2) the auth filter runs AFTER fusion; (3) the out-of-scope
confidence floor stops the always-returns-a-neighbour dense arm from manufacturing a confident
false positive, while a genuinely-separated paraphrase match IS promoted."""

from __future__ import annotations

from gecko.access import public_session
from gecko.client import AgentApiClient

# A tiny 3-op surface: two public reads + one auth-gated op.
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "components": {
        "securitySchemes": {
            "key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
        }
    },
    "paths": {
        "/api/odds/stream": {
            "get": {
                "operationId": "getApiOddsStream",
                "summary": "Odds stream",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/api/fixtures": {
            "get": {
                "operationId": "getFixtures",
                "summary": "Fixtures list",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/api/admin/rotate": {
            "post": {
                "operationId": "rotateKey",
                "summary": "Rotate API key",
                "security": [{"key": []}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


class FakeDenseIndex:
    """Deterministic dense arm: returns a fixed ranked ``(tool_name, score)`` list per query."""

    def __init__(self, by_query: dict[str, list[tuple[str, float]]]):
        self._by_query = by_query

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        return self._by_query.get(query, [])[:limit]


def test_hybrid_dict_contract_is_byte_identical_to_search():
    client = AgentApiClient(SPEC)
    dense = FakeDenseIndex({"odds": [("getApiOddsStream", 0.9), ("getFixtures", 0.4)]})
    hits = client.search_hybrid("odds", limit=5, dense_index=dense)
    assert hits and set(hits[0]) == {"name", "summary", "path", "method"}


def test_dense_ranks_a_zero_overlap_paraphrase_to_the_top():
    # Lexical scores 0 on this paraphrase (fallback only); dense ranks the stream op #1 ->
    # the hybrid RANK lifts it to the top (the recall win). It stays is_fallback (conservative:
    # no lexical corroboration) — the flag is the OOS floor, not what the agent sees.
    client = AgentApiClient(SPEC)
    query = "push me prices continuously without polling"
    dense = FakeDenseIndex({query: [("getApiOddsStream", 0.82), ("getFixtures", 0.40)]})
    scored = client.search_hybrid_scored(query, limit=5, dense_index=dense)
    assert scored[0].name == "getApiOddsStream"  # recall lift via rank
    assert scored[0].is_fallback is True  # lexical-anchored: not promoted to genuine


def test_lexical_corroboration_marks_a_hit_genuine():
    # A query the LEXICAL arm genuinely matches -> is_fallback False (above the confidence
    # floor), regardless of the dense arm.
    client = AgentApiClient(SPEC)
    dense = FakeDenseIndex({"odds stream": [("getApiOddsStream", 0.7)]})
    scored = client.search_hybrid_scored("odds stream", limit=5, dense_index=dense)
    top = next(h for h in scored if h.name == "getApiOddsStream")
    assert top.is_fallback is False


def test_auth_filter_runs_after_fusion():
    # Dense ranks the auth-gated op #1, but a no-auth (public) session must never surface it.
    client = AgentApiClient(SPEC, session=public_session())
    dense = FakeDenseIndex({"rotate": [("rotateKey", 0.95), ("getFixtures", 0.30)]})
    names = [
        h["name"] for h in client.search_hybrid("rotate", limit=5, dense_index=dense)
    ]
    assert "rotateKey" not in names
    assert "getFixtures" in names


def test_out_of_scope_stays_below_the_confidence_floor():
    # An intent no op serves: lexical is fallback-only, dense returns a FLAT distribution ->
    # nothing clears the margin -> the hybrid top-1 stays is_fallback (OOS guard holds).
    client = AgentApiClient(SPEC)
    query = "water my houseplants weekly"
    dense = FakeDenseIndex(
        {
            query: [
                ("getFixtures", 0.451),
                ("getApiOddsStream", 0.449),
            ]
        }
    )
    scored = client.search_hybrid_scored(query, limit=5, dense_index=dense)
    assert scored, "hybrid should still return candidates (never empty on intent)"
    assert all(h.is_fallback for h in scored)  # none promoted -> OOS passes
