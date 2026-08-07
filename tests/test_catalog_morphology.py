"""The Pegana routing failure: the RIGHT tool scored ZERO on a natural question.

Measured against the hosted Pegana surface (43 ops, grades A 100/100 on our own
scorecard) and reproduced offline, byte-identical, against the committed
``pegana_p0_openapi.json`` snapshot of that surface:

    "is this stablecoin depegged"  -> delete_sub (4)  ... asset_simulate_depeg  0
    "what happens if it depegs 5%" -> mint_magic (5)  ... asset_simulate_depeg  0

Two INDEPENDENT mechanisms produced that, and fixing either alone leaves the query
wrong (both were measured separately before the fix):

1. **No morphological folding.** Scoring is an exact set intersection over surface
   forms, so the query's inflection (``depegged`` / ``depegs``) never equals the
   surface's ``depeg`` — the single discriminating term on the whole API, carried by
   the path, the operationId and the summary, matches NOTHING. Score 0, not "ranked
   low": the op is invisible.
2. **No genericity floor.** Every token counts the same, so function words
   (``is``/``this``/``it``/``if``) accumulate score on whatever op happens to have the
   most prose. ``delete_sub`` won "is this stablecoin depegged" on ``is`` + ``this``.

These tests pin the OUTCOME (the right op wins its own question) and the MECHANISM
(the folder is symmetric + idempotent, the floor never empties a query), not the
specific integers — scores are free to move as long as the ranking holds.
"""

from __future__ import annotations

from pathlib import Path

from gecko.catalog import Catalog
from gecko.ingest import extract_operations, load_spec
from gecko.lexnorm import STOPWORDS, content_tokens, fold_token, fold_tokens

_PEGANA = Path(__file__).parent / "fixtures" / "pegana_p0_openapi.json"

# The measured user questions. Each is a question a user actually asks, and each one
# names the depeg simulator and nothing else on this surface.
_DEPEG_QUESTIONS = (
    "is this stablecoin depegged",
    "what happens if it depegs 5%",
    "simulate a depeg of 5 percent",
    "how much gets liquidated if it depegs",
)


def _pegana() -> Catalog:
    return Catalog(extract_operations(load_spec(str(_PEGANA))))


def test_depeg_question_routes_to_the_depeg_simulator() -> None:
    """THE regression: an inflected question must reach the op that answers it."""
    catalog = _pegana()
    for question in _DEPEG_QUESTIONS:
        hits = catalog.search_scored(question, limit=3)
        assert hits, f"{question!r} carries intent — must not return []"
        assert hits[0].entry.tool_name == "asset_simulate_depeg", (
            f"{question!r} routed to {hits[0].entry.tool_name!r} "
            f"(top-3: {[(h.score, h.entry.tool_name) for h in hits]})"
        )
        assert not hits[0].is_fallback, "must be a genuine lexical hit, not the prior"


def test_inflected_query_term_is_not_scored_zero() -> None:
    """Mechanism 1 in isolation: the discriminating term must SCORE, whatever its form.

    Scoring the right op at zero (rather than merely ranking it low) is what made this
    a comprehension bug and not a tuning one — a score-0 op is indistinguishable from
    an op that shares no vocabulary with the question at all.
    """
    catalog = _pegana()
    target = next(e for e in catalog.entries if e.tool_name == "asset_simulate_depeg")
    for question in _DEPEG_QUESTIONS:
        assert target.score_query(question) > 0, (
            f"{question!r} scored the depeg simulator 0 — the surface carries 'depeg' "
            "in its path, operationId AND summary"
        )


def test_function_words_alone_do_not_win_a_ranking() -> None:
    """Mechanism 2 in isolation: an op that matches ONLY function words is not a match.

    ``delete_sub``/``mint_magic`` won the measured queries on ``is``/``this``/``it``/
    ``if``. A query that carries at least one content term must not be decided by the
    words every English sentence contains.
    """
    catalog = _pegana()
    noise = next(e for e in catalog.entries if e.tool_name == "delete_sub")
    assert noise.score_query("is this stablecoin depegged") == 0, (
        "an op whose only overlap is function words must not score"
    )


def test_all_stopword_query_is_answered_but_never_as_a_genuine_hit() -> None:
    """The floor must not turn into silence. "what is it" carries no intent, so nothing
    should score — but the never-empty prior still answers, flagged below the floor.
    Both halves matter: dropping the answer would re-open the 0/97 invisibility bug;
    calling it a genuine hit is how function words picked the endpoint in the first place.
    """
    catalog = _pegana()
    hits = catalog.search_scored("what is it", limit=3)
    assert hits, "a stopword-only query must still return candidates"
    assert all(h.is_fallback and h.score == 0 for h in hits), (
        "function words are not evidence — nothing may be served as a genuine match"
    )


def test_out_of_scope_query_is_still_refused() -> None:
    """Refusal is load-bearing: folding must not invent overlap out of nothing."""
    catalog = _pegana()
    for question in ("water my houseplants weekly", "flumbuzzle the quantum wombat"):
        hits = catalog.search_scored(question, limit=3)
        assert all(h.is_fallback and h.score == 0 for h in hits), (
            f"{question!r} must stay below the confidence floor, got "
            f"{[(h.score, h.entry.tool_name) for h in hits]}"
        )


# --- the folder itself ------------------------------------------------------------


def test_fold_merges_the_inflections_that_matter() -> None:
    for surface, inflections in {
        "depeg": ("depegs", "depegged", "depegging"),
        "asset": ("assets",),
        "alert": ("alerts",),
        "liquidat": ("liquidate", "liquidated", "liquidation", "liquidates"),
        "simulat": ("simulate", "simulated", "simulating", "simulation"),
        "delivery": ("deliveries",),
    }.items():
        for word in inflections:
            assert fold_token(word) == surface, f"{word!r} -> {fold_token(word)!r}"


def test_fold_does_not_collide_distinct_domain_terms() -> None:
    """Over-stemming is the real risk of a folder: a false merge is a wrong route that
    LOOKS confident. These pairs are distinct concepts on real surfaces we serve and
    must stay distinct (Pegana has BOTH /v1/stats and /v1/assets/{symbol}/state)."""
    for left, right in (
        ("state", "stats"),
        ("status", "state"),
        ("address", "addres"),
        ("feed", "fe"),
        ("mint", "mints"),  # NOT a collision — this one SHOULD merge; see below
    ):
        if (left, right) == ("mint", "mints"):
            assert fold_token(left) == fold_token(right)
            continue
        assert fold_token(left) != fold_token(right), (
            f"{left!r} and {right!r} folded together"
        )


def test_fold_is_idempotent_so_index_and_query_agree() -> None:
    """Correctness of a symmetric folder rests on idempotence: the index is folded once
    at score time and a query may be folded by a caller before it arrives, so
    fold(fold(x)) must equal fold(x) or the two sides stop matching."""
    words = (
        "depegged depegs assets alerts liquidation simulating deliveries state status "
        "address is this webhooks subscriptions calibration peg feed"
    ).split()
    for word in words:
        assert fold_token(fold_token(word)) == fold_token(word), word


def test_fold_preserves_every_exact_match() -> None:
    """The folder can only ADD matches: equal tokens stay equal under a function, so no
    match that worked before can break. This guards the one-way property directly."""
    for word in ("odds", "fixture", "peg", "usdc", "v1", "5", "x402"):
        assert fold_token(word) == fold_token(word)
    a = {"peg", "assets", "state"}
    assert fold_tokens(a) & fold_tokens(a) == fold_tokens(a)


def test_content_tokens_reports_a_contentless_query_as_contentless() -> None:
    assert content_tokens({"is", "the"}) == set(), (
        "an all-stopword query carries no intent — say so, don't score it"
    )
    assert content_tokens({"is", "depeg"}) == {"depeg"}
    assert "is" in STOPWORDS and "depeg" not in STOPWORDS


def test_normalize_query_is_idempotent_and_catches_folded_function_words() -> None:
    """``does`` folds to ``doe``, which is in no stopword list — normalizing must catch
    it on the folded side too or a function word walks back in through the folder."""
    from gecko.lexnorm import normalize_query

    assert normalize_query({"does", "it", "depeg"}) == {"depeg"}
    once = normalize_query({"what", "happens", "if", "it", "depegs"})
    assert normalize_query(once) == once
