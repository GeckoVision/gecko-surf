"""The blurb is a four-field document, and the fallback prior is not fusion evidence.

Two defects, one theme: a structure was being consumed as an undifferentiated bag.

**The blurb.** It is generated as four XML tags inside a markdown fence and was folded into
the index as a raw string, so `xml`, `intent`, `required`, `auth`, `gotchas` and the literal
`none` entered the surface text of EVERY enriched operation. Uniform across the surface, so
useless for ranking — and actively harmful for GATING, because `intent_tokens()` is what
certifies a query as in scope. The query "none" returned three genuine in-scope hits on
pegana, re-opening the out-of-scope hole closed in 8ccb306 one commit earlier.

**The fusion.** `hybrid_scored` fed RRF the FULL lexical list. On a zero-overlap query that
list is `Catalog.search_scored`'s never-empty 0/97 prior — GET-first, then path — which is
query-INDEPENDENT: byte-identical for every query on the surface. RRF was therefore
averaging the dense arm's answer against a constant, and it cost real rank on exactly the
paraphrase queries the dense arm exists to rescue.
"""

from __future__ import annotations

import pytest

from gecko.access import public_session
from gecko.catalog import Catalog
from gecko.client import AgentApiClient
from gecko.enrich import load_pinned_blurbs, parse_blurb
from gecko.fusion import RRF_K
from gecko.ingest import extract_operations, load_spec
from gecko.search import hybrid_scored
from gecko.tools import tool_name

PEGANA = "tests/fixtures/pegana_openapi.json"
BLURBS = "tests/fixtures/golden/blurbs/pegana.json"

#: Every token the blurb's SCAFFOLDING contributes, none of which is anything a user asks
#: about. `none` is the generator's absence marker for an empty field.
SCAFFOLD = ["xml", "intent", "required", "gotchas", "none"]


@pytest.fixture(scope="module")
def enriched() -> AgentApiClient:
    return AgentApiClient(
        PEGANA, session=public_session(), blurbs=load_pinned_blurbs(BLURBS)
    )


# --- 1. the blurb parses into fields --------------------------------------------


def test_the_pinned_blurbs_really_are_fenced_xml() -> None:
    """Guard against this whole file going vacuous: if the pinned format ever stops being
    fenced XML, the leak it defends against is gone and these tests prove nothing."""
    raw = list(load_pinned_blurbs(BLURBS).values())
    assert raw, "no pinned blurbs"
    assert any(r.lstrip().startswith("```") for r in raw), "no fenced blurb left"
    assert all("<intent>" in r for r in raw), "the four-tag format changed"


def test_parse_keeps_bodies_and_drops_markup() -> None:
    fields = parse_blurb(
        "```xml\n<intent>find a thing</intent>\n<auth>none</auth>\n```"
    )
    assert fields.intent == "find a thing"
    assert fields.auth == "", "'none' is an absence marker, not content"
    assert "xml" not in fields.ranking_text and "<" not in fields.ranking_text


@pytest.mark.parametrize(
    "raw", ["", "not xml at all", "<intent>unterminated", "```xml\n```"]
)
def test_parse_never_raises_on_a_malformed_blurb(raw: str) -> None:
    # A truncated generation or a sanitizer that failed closed must degrade to "no blurb",
    # never to an exception on the retrieval hot path.
    assert parse_blurb(raw).ranking_text == ""


# --- 2. scaffolding cannot certify a query as in scope --------------------------


@pytest.mark.parametrize("word", SCAFFOLD)
def test_scaffold_vocabulary_certifies_nothing(
    enriched: AgentApiClient, word: str
) -> None:
    genuine = [h for h in enriched.search_scored(word, 5) if not h.is_fallback]
    assert not genuine, f"{word!r} certified {[h.name for h in genuine]} as in scope"


def test_scaffold_vocabulary_reaches_no_operations_intent_surface(
    enriched: AgentApiClient,
) -> None:
    """The stronger form: not "nothing ranked" but "the gate surface never contains these".
    A query-level assertion could pass because a word happened to rank nothing."""
    for entry in enriched.catalog.entries:
        surface = entry.intent_tokens()
        for word in SCAFFOLD:
            assert word not in surface, f"{word!r} in {entry.tool_name}'s gate surface"


def test_a_real_intent_still_certifies(enriched: AgentApiClient) -> None:
    top = enriched.search_scored("list all active assets", 3)[0]
    assert not top.is_fallback and top.name == "list_assets"


def test_the_blurb_still_contributes_its_intent_text(enriched: AgentApiClient) -> None:
    """Parsing must not amount to throwing the enrichment away: the words a user would
    actually type still have to reach the gate."""
    by_name = {e.tool_name: e for e in enriched.catalog.entries}
    with_intent = [e for e in by_name.values() if e._blurb_fields.intent]
    assert with_intent, "no pinned blurb carried an <intent> body"
    entry = with_intent[0]
    word = next(
        w
        for w in entry._blurb_fields.intent.lower().split()
        if len(w) > 5 and w not in entry.operation.summary.lower()
    )
    assert word in entry.intent_tokens() or any(
        word.startswith(t) for t in entry.intent_tokens()
    ), f"{word!r} from <intent> never reached the gate surface"


# --- 3. the never-empty prior is not fusion evidence ----------------------------


class _FakeDense:
    """Ranks by an explicit name order. A fake, not a mock: the dense arm's whole contract
    is `query -> ranked names`, so a list IS the arm."""

    def __init__(self, order: list[str]):
        self._order = order

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        return [(n, 1.0 - i / 100) for i, n in enumerate(self._order)][:limit]


def _pegana_catalog() -> tuple[Catalog, dict[str, object], set[str]]:
    ops = extract_operations(load_spec(PEGANA))
    by_name = {tool_name(o): o for o in ops}
    return Catalog(ops), by_name, set(by_name)


def test_a_zero_overlap_query_returns_the_dense_ranking_intact() -> None:
    catalog, by_name, usable = _pegana_catalog()
    target = sorted(usable)[7]

    # A query with no lexical overlap anywhere: the lexical arm can only serve its prior.
    query = "zzqqxx nonsensical vocabulary"
    assert all(s.is_fallback for s in catalog.search_scored(query, 25)), (
        "this query was supposed to have zero lexical overlap"
    )

    hits = hybrid_scored(
        catalog,
        by_name,  # type: ignore[arg-type]
        usable,
        query,
        limit=5,
        dense_index=_FakeDense([target] + sorted(usable - {target})),
        k=RRF_K,
    )
    assert hits[0].name == target, (
        "the dense arm's top hit was displaced by the query-independent lexical prior"
    )
    # Scope is still refused — the dense arm ranks, it never certifies.
    assert all(h.is_fallback for h in hits)


def test_the_never_empty_contract_survives_an_empty_dense_arm() -> None:
    catalog, by_name, usable = _pegana_catalog()
    hits = hybrid_scored(
        catalog,
        by_name,  # type: ignore[arg-type]
        usable,
        "zzqqxx nonsensical vocabulary",
        limit=5,
        dense_index=_FakeDense([]),
        k=RRF_K,
    )
    assert hits, "both arms empty must still yield the flagged prior, never []"
    assert all(h.is_fallback for h in hits)


def test_a_lexically_certified_query_still_fuses_both_arms() -> None:
    catalog, by_name, usable = _pegana_catalog()
    lex_top = catalog.search_scored("list all active assets", 5)[0].entry.tool_name
    other = sorted(usable - {lex_top})[0]

    hits = hybrid_scored(
        catalog,
        by_name,  # type: ignore[arg-type]
        usable,
        "list all active assets",
        limit=10,
        dense_index=_FakeDense([other, lex_top]),
        k=RRF_K,
    )
    names = [h.name for h in hits]
    # Both arms contributed: the dense-only name is present, and the lexically-certified
    # one keeps its genuine flag.
    assert other in names and lex_top in names
    assert next(h for h in hits if h.name == lex_top).is_fallback is False


# --- 4. the tool name is a join key, so it must be injective --------------------


def test_a_truncated_tool_name_stays_distinct() -> None:
    """Two operationIds identical for their first 64 characters must NOT collapse.

    Before the digest they both truncated to the same name, and because that name is the
    join key, a query for one resolved to the other — a wrong call, not a bad rank.
    """
    from gecko.tools import safe_tool_name

    shared = "get" + "Extremely" * 8  # > 64 chars before either suffix
    a, b = safe_tool_name(shared + "V1"), safe_tool_name(shared + "V2")
    assert a != b, "two distinct operations still share one tool name"
    assert len(a) <= 64 and len(b) <= 64
    # Deterministic and independent of siblings: same id in, same name out, every time.
    assert safe_tool_name(shared + "V1") == a


def test_short_names_are_untouched_by_the_digest() -> None:
    """The disambiguator must not churn the names of every existing surface."""
    from gecko.tools import safe_tool_name

    for raw in ("list_assets", "get-defi-price", "postAuthGuestStart"):
        assert safe_tool_name(raw) == raw


def test_a_surface_whose_names_collide_refuses_to_build() -> None:
    """The residual case the per-op digest cannot fix: two ids differing only in
    characters the sanitizer folds to `_`. Serving that surface would silently resolve one
    operation's requests to the other, so construction refuses instead."""
    from gecko.client import ToolNameCollision

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a": {"get": {"operationId": "get.thing", "responses": {}}},
            "/b": {"get": {"operationId": "get thing", "responses": {}}},
        },
    }
    with pytest.raises(ToolNameCollision) as excinfo:
        AgentApiClient(spec, session=public_session())
    # The message must name BOTH originals — otherwise it cannot be acted on.
    assert "get.thing" in str(excinfo.value) and "get thing" in str(excinfo.value)
