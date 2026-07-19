"""Search / rank / fusion substrate behind ``AgentApiClient``.

Split out of ``client.py`` so retrieval — the lexical top-k ranker, the below-scale
surface-all rule, and hybrid RRF fusion — evolves independently of the call path the
graph work keeps extending. Pure functions over exactly the comprehension state they
rank (catalog, usable-tool set, op index): no I/O, no auth, no client back-reference,
so this module can never re-import ``client`` (no cycle) and stays falsifiable offline.

``AgentApiClient`` keeps thin, signature-stable delegators; every WHY lives here with
the logic it explains.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .fusion import RRF_K, rrf_fuse

if TYPE_CHECKING:
    from .catalog import Catalog
    from .dense import DenseIndex
    from .ingest import Operation


@dataclass(frozen=True)
class ScoredHit:
    """A search result enriched with retrieval provenance — the introspection sibling
    of the frozen ``search`` dict shape. ``score``/``is_fallback`` power retrieval
    evaluation and the out-of-scope confidence floor; the agent-facing ``search`` never
    exposes them (its contract stays ``{name, summary, path, method}``)."""

    name: str
    summary: str
    path: str
    method: str
    score: int
    is_fallback: bool


@dataclass(frozen=True)
class FusedHit:
    """A hybrid (lexical+dense) search result with fusion provenance — the scored sibling of
    the frozen ``search_hybrid`` dict shape. ``score`` is the RRF score (drives order/recall).
    ``is_fallback`` is the OOS confidence floor and is LEXICAL-ANCHORED: True unless the
    lexical arm genuinely corroborated the hit (``score > 0``). The dense arm improves the
    RANKING but never sets confidence on its own — measured on ``voyage-4-lite``, its cosine
    scores are too compressed to separate an out-of-scope intent from a real paraphrase, so
    tying confidence to lexical corroboration guarantees OOS pass-rate >= the lexical baseline
    by construction, while dense still lifts paraphrase recall via rank."""

    name: str
    summary: str
    path: str
    method: str
    score: float
    is_fallback: bool


def project_hits(hits: Sequence[ScoredHit | FusedHit]) -> list[dict[str, Any]]:
    """Project scored hits down to the frozen agent-facing dict shape
    (``{name, summary, path, method}``) — score/fallback provenance stays internal."""
    return [
        {"name": h.name, "summary": h.summary, "path": h.path, "method": h.method}
        for h in hits
    ]


def search_scored(
    catalog: Catalog,
    usable_tool_names: set[str],
    query: str,
    limit: int,
) -> list[ScoredHit]:
    """The pure ranked retrieval substrate — carries ``score``/``is_fallback`` (retrieval
    eval + the out-of-scope confidence floor). Applies the auth filter and top-k over-
    fetch. This is what the retrieval benchmark measures (recall@k / MRR), so it stays a
    strict top-k ranker even below scale; the agent-facing ``search`` layers the below-
    scale surface-all rule on TOP of it (see ``surface_all_scored``)."""
    out: list[ScoredHit] = []
    for s in catalog.search_scored(query, limit + 20):
        if s.entry.tool_name not in usable_tool_names:
            continue
        out.append(
            ScoredHit(
                name=s.entry.tool_name,
                summary=s.entry.operation.summary,
                path=s.entry.operation.path,
                method=s.entry.operation.method,
                score=s.score,
                is_fallback=s.is_fallback,
            )
        )
        if len(out) >= limit:
            break
    return out


def surface_all_scored(
    catalog: Catalog,
    ops_by_name: Mapping[str, Operation],
    usable_tool_names: set[str],
    query: str,
) -> list[ScoredHit]:
    """Below-scale: surface EVERY usable tool (no top-k truncation) so Gecko is never
    worse than the raw OpenAPI dump. Genuine lexical hits keep their relevance order and
    score; every remaining usable op is APPENDED as a score-0 fallback (GET-first then
    path — the catalog's query-independent prior), so a zero-overlap paraphrase op the
    lexical catalog structurally drops is still visible and pickable. ``is_fallback``
    stays truthful (appended ops are not genuine lexical matches), so any confidence-floor
    reader is unchanged and relevance never sinks below a manufactured candidate."""
    hits: list[ScoredHit] = []
    seen: set[str] = set()
    # Genuine lexical hits first, over the full usable pool (depth = #entries so nothing
    # is censored). Skip fallbacks here — we append the not-yet-seen ops ourselves below.
    for s in catalog.search_scored(query, len(catalog.entries)):
        name = s.entry.tool_name
        if name not in usable_tool_names or s.is_fallback:
            continue
        seen.add(name)
        op = s.entry.operation
        hits.append(ScoredHit(name, op.summary, op.path, op.method, s.score, False))
    remaining = [
        (name, op)
        for name, op in ops_by_name.items()
        if name in usable_tool_names and name not in seen
    ]
    remaining.sort(key=lambda no: (0 if no[1].method == "GET" else 1, no[1].path))
    for name, op in remaining:
        hits.append(ScoredHit(name, op.summary, op.path, op.method, 0, True))
    return hits


def ranked_hits(
    catalog: Catalog,
    ops_by_name: Mapping[str, Operation],
    usable_tool_names: set[str],
    surface_all: bool,
    query: str,
    limit: int,
) -> list[ScoredHit]:
    """The provenance-carrying substrate of the agent-facing ``search``: the surface-all
    branch below scale, the strict top-k ranker above it. ``search`` is a pure projection
    of this, so the frozen dict shape and the scored view can never disagree on order —
    and a caller that needs the top hit's ``is_fallback`` (the genuine-hit gate for plan
    attachment) reads it from HERE instead of re-running retrieval."""
    if surface_all:
        return surface_all_scored(catalog, ops_by_name, usable_tool_names, query)
    return search_scored(catalog, usable_tool_names, query, limit)


def hybrid_scored(
    catalog: Catalog,
    ops_by_name: Mapping[str, Operation],
    usable_tool_names: set[str],
    query: str,
    limit: int,
    *,
    dense_index: DenseIndex,
    k: int = RRF_K,
) -> list[FusedHit]:
    """Fuse the lexical arm (``catalog.search_scored``) with the injected dense arm via
    RRF, joined on ``tool_name``. Over-fetches both arms, fuses, applies the auth filter
    AFTER fusion, then truncates to ``limit`` (so reranking/hiding can't starve the top-k).
    ``search_hybrid`` is a pure projection of this — the two can never disagree on order.

    ``is_fallback`` is LEXICAL-ANCHORED (genuine iff the lexical arm scored the op > 0),
    the out-of-scope confidence floor: an OOS intent has no lexical overlap so nothing is
    promoted -> OOS pass-rate >= the lexical baseline by construction. The dense arm still
    lifts paraphrase recall because RANK (not the flag) drives recall.
    """
    depth = limit + 20
    lex = catalog.search_scored(query, depth)
    lex_names = [s.entry.tool_name for s in lex]
    lex_genuine = {s.entry.tool_name for s in lex if not s.is_fallback}

    dense_names = [n for n, _ in dense_index.search(query, depth)]

    fused = rrf_fuse([lex_names, dense_names], k)
    # Deterministic order: RRF score desc, then tool_name for stable ties.
    ranked = sorted(fused.items(), key=lambda ns: (-ns[1], ns[0]))

    out: list[FusedHit] = []
    for name, score in ranked:
        if name not in usable_tool_names:  # auth filter AFTER fusion
            continue
        op = ops_by_name.get(name)
        if op is None:  # a stale dense doc for an op no longer on the surface
            continue
        out.append(
            FusedHit(
                name=name,
                summary=op.summary,
                path=op.path,
                method=op.method,
                score=score,
                is_fallback=name not in lex_genuine,
            )
        )
        if len(out) >= limit:
            break
    return out
