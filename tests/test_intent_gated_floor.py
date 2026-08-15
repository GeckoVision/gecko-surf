"""The out-of-scope floor gates on INTENT vocabulary, not on reference prose.

The defect, measured on an 89-op surface: the floor asked only "did any operation score
above zero anywhere in its haystack", and the haystack includes `description`. Reference
prose narrates schema internals in ordinary English, so two nonsense queries certified
themselves as in-scope on one token each:

    "recommend nearby vegan restaurants OPEN tonight"  -> the OHLC candle's *open value*
    "book me a flight to Lisbon NEXT Tuesday"          -> `next_scroll_id`

Neither token is ubiquitous — both appear in exactly 1 of 89 operations — so no IDF or
document-frequency weighting would have caught them. Rarity is what made them look
significant. The distinction that works is not how OFTEN a token appears but WHICH FIELD
it appears in: description prose is legitimate ranking evidence and bad gating evidence.

So the catalog now ranks on the whole haystack and gates on summary/tags/operationId/blurb.
"""

from __future__ import annotations

import pytest

from gecko.access import Session
from gecko.catalog import _tokens
from gecko.client import AgentApiClient

SPEC = "examples/birdeye_demo/spec/birdeye_openapi.json"

#: The two leaks, with the token each one rode in on. Kept as data so the reason a query is
#: here is legible, and so a future reader can re-derive the failure rather than trust it.
LEAKS = [
    ("recommend nearby vegan restaurants open tonight", "open"),
    ("book me a flight to Lisbon next Tuesday", "next"),
]


@pytest.fixture(scope="module")
def client() -> AgentApiClient:
    return AgentApiClient(SPEC, session=Session(jwt="recorded", api_token="recorded"))


@pytest.mark.parametrize(("query", "token"), LEAKS)
def test_prose_only_matches_do_not_certify_an_out_of_scope_query(
    client: AgentApiClient, query: str, token: str
) -> None:
    top = client.search_scored(query, 3)[0]
    assert top.is_fallback, (
        f"{query!r} was certified in-scope by {top.name} — the floor accepted a match "
        f"won on reference prose"
    )


@pytest.mark.parametrize(("query", "token"), LEAKS)
def test_the_leak_is_real_and_still_reachable_by_ranking(
    client: AgentApiClient, query: str, token: str
) -> None:
    """The guard above must be closing a REAL hole, not passing because the token stopped
    matching. The lexical overlap that caused the leak is still there — ranking is
    unchanged; only its authority to certify scope was removed."""
    entries = client.catalog.entries
    scoring = [e for e in entries if e.score(_tokens(query)) > 0]
    assert scoring, (
        f"{query!r} no longer overlaps anything — this test has gone vacuous"
    )
    # ...and every one of them is prose-only, which is exactly why the gate rejects them.
    assert all(e.intent_score(_tokens(query)) == 0 for e in scoring)


def test_an_in_scope_query_is_still_certified(client: AgentApiClient) -> None:
    # The gate must not be a blanket refusal: a real intent still passes it.
    top = client.search_scored("trending token list", 3)[0]
    assert not top.is_fallback
    assert top.score > 0


def test_the_gate_is_not_the_stopword_filter_wearing_a_hat(
    client: AgentApiClient,
) -> None:
    """`open` and `next` are ordinary content words, not stopwords — the existing genericity
    floor cannot be what rejects them, so this is a distinct mechanism."""
    from gecko.lexnorm import STOPWORDS

    for _, token in LEAKS:
        assert token not in STOPWORDS
