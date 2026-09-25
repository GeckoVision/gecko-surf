"""The rankable unit — the thing the shipped lexical arm actually scores.

WHY THIS EXISTS. The ranker has never touched the three fields that make an
``Operation`` an OpenAPI thing. ``catalog.CatalogEntry._haystack`` reads summary,
description, path, tags, operationId and the blurb; ``intent_tokens`` reads a
subset of those. Method, parameters and responses — everything that makes an
operation CALLABLE — are invisible to it. So the scorer is not API-shaped; only
its carrier type was.

That mattered the moment a second kind of thing needed ranking. The two in-repo
precedents (``find_start.py`` and ``paysh_catalog.py``) fabricate an ``Operation``
to reuse this scorer, and for a program instruction or an x402 service that is
honest: both ARE callable. A course page is not. A fabricated ``Operation`` for a
lesson would flow into ``tools.to_tool`` and ``caller.prepare`` and the type
system would say nothing, because a fabricated op is callable by type.

So: ONE frozen unit, TWO projectors. ``CatalogEntry`` projects an ``Operation``
into a unit (:meth:`gecko.catalog.CatalogEntry.as_unit`); ``gecko.doccorpus``
projects a document chunk into one. Nothing below this line knows which it got,
and nothing above it can mistake a page for a call.

THE FIELDS, and why each one is here rather than a bag of text:

``title``     the intent-bearing headline (an op's summary, a page's title +
              heading path). RANKED TWICE — the shipped double-count.
``identity``  the unit's own name (operationId, page id). RANKED TWICE for the
              same reason: the identifier's sub-words are what an intent that
              names this unit overlaps.
``body``      reference prose. Ranked, but see ``gate`` below — on an API surface
              a body-only match is not evidence the query is in scope.
``locator``   where the unit lives (path, page id). Ranked, and the deterministic
              tie-break, so two units with equal scores order identically to the
              way they always have.
``tags``      grouping vocabulary. Ranked and gating.
``aux_rank`` / ``aux_intent``
              the enrichment blurb's two readings: its whole ranking text, and
              only its ``<intent>`` body for gating. Kept apart because folding
              the whole blurb into the gate let the query "none" certify three
              in-scope hits on pegana.

TWO POLICIES, BOTH PASSED IN, NEITHER ASSUMED:

``gate``      require a match on the intent surface before a hit may certify the
              query as in-scope. ON for API operations (it took out-of-scope pass
              on an 89-op surface from 0.33 to 1.00). OFF for documents, where the
              body IS the answer and gating on the title would refuse every page
              that teaches something its heading does not name.
``fallback``  the never-empty 0/97 prior. ON for API operations, where returning
              nothing was a shipped discovery bug. OFF for documents, where "not
              in these pages" is the honest answer and a query-independent prior
              would hand a student a week-2 page for a week-9 question.

The tokenizer is INJECTED, never imported here. ``catalog._tokens`` is a module
global the retrieval-arms harness swaps at runtime to compare tokenizers; an
import would bind one vocabulary at import time and quietly serve it to every
arm.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from .lexnorm import fold_tokens, normalize_query

__all__ = [
    "FoldedUnit",
    "RankableUnit",
    "RankedUnit",
    "Tokenize",
    "fold_unit",
    "intent_score_folded",
    "rank_units",
    "score_folded",
    "unit_text",
]

#: Text -> its index/query tokens. ``catalog._tokens`` is the shipped one.
Tokenize = Callable[[str], set[str]]


@dataclass(frozen=True)
class RankableUnit:
    """One thing the lexical arm can rank. NOT one thing an agent can call."""

    unit_id: str
    title: str
    body: str = ""
    locator: str = ""
    identity: str = ""
    tags: tuple[str, ...] = ()
    aux_rank: str = ""
    aux_intent: str = ""
    #: Ordering class for the never-empty fallback (an op: GET first, then the
    #: rest). Only read when ``fallback`` is on, which documents never turn on.
    fallback_rank: int = 0


def unit_text(unit: RankableUnit) -> str:
    """The single glued haystack, in the field order the overlap scorer has always
    used. Order is irrelevant to the result (it becomes a set) and is preserved so
    the two implementations can be diffed by eye."""
    return " ".join(
        [
            unit.title,
            unit.body,
            unit.locator,
            " ".join(unit.tags),
            unit.identity,
            unit.aux_rank,
        ]
    )


@dataclass(frozen=True)
class FoldedUnit:
    """A unit with its four scored surfaces folded once.

    Derived per call on the API path (``catalog`` rebuilds it every search, because
    the tokenizer is a swappable global and a per-entry cache would serve one arm's
    vocabulary to the next) and ONCE at build time on the document path, where the
    corpus is static and 879 chunks x 49 queries is 43,000 re-folds otherwise.
    """

    unit: RankableUnit
    haystack: frozenset[str] = field(default_factory=frozenset)
    title: frozenset[str] = field(default_factory=frozenset)
    identity: frozenset[str] = field(default_factory=frozenset)
    intent: frozenset[str] = field(default_factory=frozenset)


def fold_unit(unit: RankableUnit, tokenize: Tokenize) -> FoldedUnit:
    """Tokenize and fold a unit's four scored surfaces."""
    intent_text = " ".join(
        [unit.title, " ".join(unit.tags), unit.identity, unit.aux_intent]
    )
    return FoldedUnit(
        unit=unit,
        haystack=frozenset(fold_tokens(tokenize(unit_text(unit)))),
        title=frozenset(fold_tokens(tokenize(unit.title))),
        identity=frozenset(fold_tokens(tokenize(unit.identity))),
        intent=frozenset(fold_tokens(tokenize(intent_text))),
    )


def score_folded(folded: FoldedUnit, query_tokens: set[str]) -> int:
    """Overlap over the query's content terms; title and identity count twice.

    ``normalize_query`` is applied HERE rather than at the call site so every
    caller ranks on the same vocabulary. It is idempotent, so a caller that
    already folded gets the identical result.
    """
    query_tokens = normalize_query(query_tokens)
    if not query_tokens:
        # No content-bearing term survived (an all-stopword query). Scoring 0 is
        # the honest answer.
        return 0
    return (
        len(query_tokens & folded.haystack)
        + len(query_tokens & folded.title)
        + len(query_tokens & folded.identity)
    )


def score_folded_weighted(
    folded: FoldedUnit, query_tokens: set[str], weight: Mapping[str, float]
) -> float:
    """``score_folded``, with each matched term contributing its own weight.

    ADDED BESIDE rather than folded into ``score_folded`` on purpose. That function
    returns an ``int`` and four committed scorecards are pinned to the integers it
    produces; giving it a weight would change every number the API path has ever
    published, to fix a problem only the document path has.

    The problem it fixes, measured 2026-09-24 on the 241-document course corpus:
    counting DISTINCT matched terms makes "redact" (19 pages) worth exactly what
    "write" (most pages) is worth, so the notebook that teaches `redact` ranked 24th
    behind two dozen pages tied at the same score. A term's weight is how much its
    presence narrows the field, which is what an IDF is.

    Same three surfaces and the same double-count as ``score_folded``, so the only
    difference between the two is what a match is worth. A term absent from
    ``weight`` contributes nothing, which is the honest treatment of a term the
    corpus has never seen.
    """
    query_tokens = normalize_query(query_tokens)
    if not query_tokens:
        return 0.0
    return sum(
        weight.get(term, 0.0)
        for surface in (folded.haystack, folded.title, folded.identity)
        for term in query_tokens & surface
    )


def intent_score_folded(folded: FoldedUnit, query_tokens: set[str]) -> int:
    """Corroboration on the intent surface. ``0`` means: this unit ranked only
    because the query brushed its reference prose, which is not evidence that the
    query is in scope."""
    return len(normalize_query(query_tokens) & folded.intent)


@dataclass(frozen=True)
class RankedUnit:
    """A position in the ranking, by index into the folded sequence the caller
    passed. Returning indices rather than units is what lets ``Catalog`` map a
    result back to its own ``CatalogEntry`` without this module knowing that type
    exists."""

    index: int
    score: int
    is_fallback: bool


def rank_units(
    folded: Sequence[FoldedUnit],
    query_tokens: set[str],
    limit: int = 5,
    *,
    gate: bool,
    fallback: bool,
) -> list[RankedUnit]:
    """Rank folded units for a query, under two explicitly-passed policies.

    Genuine hits (``score > 0``, and — when ``gate`` is on — corroborated on the
    intent surface) rank first, by score then by ``locator`` for a deterministic
    tie-break. With no genuine hit: the query-independent prior when ``fallback``
    is on (flagged ``is_fallback`` at score 0, so a confidence floor can keep it
    out), and ``[]`` when it is off.
    """
    if not query_tokens:
        return []
    scored = [(score_folded(f, query_tokens), index) for index, f in enumerate(folded)]
    matches = sorted(
        (item for item in scored if item[0] > 0),
        key=lambda item: (-item[0], folded[item[1]].unit.locator),
    )
    # Rank on everything, GATE on intent. A hit won purely inside reference prose
    # is ranked exactly where it was, but is not allowed to certify the query as
    # in-scope.
    if gate:
        matches = [
            item
            for item in matches
            if intent_score_folded(folded[item[1]], query_tokens) > 0
        ]
    if matches:
        return [RankedUnit(index, score, False) for score, index in matches[:limit]]
    if not fallback:
        return []
    order = sorted(
        range(len(folded)),
        key=lambda index: (
            folded[index].unit.fallback_rank,
            folded[index].unit.locator,
        ),
    )
    return [RankedUnit(index, 0, True) for index in order[:limit]]
