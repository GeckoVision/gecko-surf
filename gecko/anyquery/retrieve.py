"""Query -> the top-``k`` catalog candidates the planner is shown (``CandidatesFor``).

Lexical, on purpose: :class:`gecko.catalog.BM25Index` over the candidates projected into
the ``Operation`` shape it already ranks, with the query widened by a short table of
business-data synonyms (``hiring`` -> job openings).

Measured on the 8-query golden set against the 906-endpoint pull (448 in the pool), as
the share of planned fields with an acceptable source in the top ``k``: whole query
15/19 at k=20 and 18/19 at k=40; with the synonyms 18/19 and 19/19. Splitting the query
into clauses and interleaving was tried and measured WORSE (16/19 at k=20), so it is not
here. The synonym table was written while looking at those misses: the numbers are
in-sample until a held-out paraphrase set exists.

A hit the index flags ``is_fallback`` (no genuine overlap) is never returned: an empty
candidate list is the honest answer, and it becomes a refusal upstream.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from ..catalog import BM25Index, CatalogEntry
from ..ingest import Operation
from .candidates import snapshot_sha
from .types import CandidateEndpoint, CandidateSet, PlanRequest

__all__ = ["bm25_candidates", "expand"]

_PATH_WORDS = re.compile(r"[\[\]\._\-/{}]+")
#: How people ask -> how catalogs describe it. Business-data vocabulary only, added to
#: the query (never replacing a word), so a match that worked without it still works.
_EXPAND: dict[str, str] = {
    "hiring": "job openings postings jobs",
    "hires": "job openings postings jobs",
    "funding": "financing funding rounds investment",
    "raised": "financing funding rounds",
    "headcount": "employees employee count workforce",
    "staff": "employees",
    "parent": "parent company hierarchy subsidiaries",
    "owner": "parent company hierarchy beneficial ownership",
    "price": "price snapshot quote",
    "deliverable": "email verifier verification deliverability",
    "stack": "technologies technology",
    "competitors": "similar companies peers",
    "lookalikes": "similar companies",
}
_INDEX_CACHE: OrderedDict[str, BM25Index] = OrderedDict()
_CACHE_SIZE = 4


def expand(text: str) -> str:
    """``text`` plus the catalog words for any intent word it uses."""
    extra = [_EXPAND[w] for w in re.findall(r"[a-z]+", text.lower()) if w in _EXPAND]
    return " ".join([text, *extra])


def _operation(candidate: CandidateEndpoint) -> Operation:
    fields = " ".join(_PATH_WORDS.sub(" ", p) for p in candidate.response_paths[:60])
    inputs = " ".join(sorted(candidate.query_keys | candidate.body_keys))
    return Operation(
        method=candidate.method,
        path=candidate.path,
        operation_id=candidate.candidate_id,
        summary=candidate.description,
        description=f"{inputs} {fields}",
        tags=[candidate.api],
        parameters=[],
        request_body=None,
        responses={},
    )


def _index(catalog: CandidateSet) -> BM25Index:
    # Keyed on the ids and prices themselves: a caller-supplied sha is not trusted to be
    # unique (a test may build two different sets with the same placeholder sha).
    key = snapshot_sha(catalog.candidates)
    found = _INDEX_CACHE.get(key)
    if found is not None:
        _INDEX_CACHE.move_to_end(key)
        return found
    built = BM25Index([CatalogEntry(_operation(c)) for c in catalog.candidates])
    _INDEX_CACHE[key] = built
    while len(_INDEX_CACHE) > _CACHE_SIZE:
        _INDEX_CACHE.popitem(last=False)
    return built


def _ranked(index: BM25Index, text: str, limit: int) -> list[str]:
    hits = index.search_scored(text, limit=limit)
    return [h.entry.operation.operation_id for h in hits if not h.is_fallback]


def bm25_candidates(
    request: PlanRequest, catalog: CandidateSet, k: int = 40
) -> CandidateSet:
    """The top ``k`` genuine BM25 hits for the widened query, best first."""
    if k <= 0 or not catalog.candidates:
        return CandidateSet((), snapshot_sha(()))
    chosen = _ranked(_index(catalog), expand(request.query), k)
    by_id = {c.candidate_id: c for c in catalog.candidates}
    picked = tuple(by_id[cid] for cid in chosen[:k])
    return CandidateSet(picked, snapshot_sha(picked))
