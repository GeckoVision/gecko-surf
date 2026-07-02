"""Lightweight capability catalog — the "find the right endpoint" layer.

Lexical search over the operations' surface text. At ~tens of endpoints this is
more accurate and far simpler than vector RAG; vectorization is the multi-API /
large-API play (V2), deliberately deferred.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from .ingest import Operation
from .tools import tool_name

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


@dataclass
class CatalogEntry:
    operation: Operation
    # S0 enrich: an OPTIONAL, pre-generated situating blurb folded into the overlap surface
    # (intent vocabulary a user searches with). Pure DATA — no LLM/SDK reaches this module
    # (invariant #2). Empty by default, so the plain lexical baseline is unchanged.
    blurb: str = ""

    @property
    def tool_name(self) -> str:
        # Must match to_tool()["name"] exactly — client.search filters on it.
        return tool_name(self.operation)

    @property
    def _haystack(self) -> str:
        o = self.operation
        return " ".join(
            [
                o.summary,
                o.description,
                o.path,
                " ".join(o.tags),
                o.operation_id,
                self.blurb,
            ]
        )

    def score(self, query_tokens: set[str]) -> int:
        if not query_tokens:
            return 0
        hay = _tokens(self._haystack)
        summary = _tokens(self.operation.summary)
        # summary matches count double (the most intent-bearing field)
        return len(query_tokens & hay) + len(query_tokens & summary)


@dataclass(frozen=True)
class ScoredEntry:
    """A catalog hit plus its retrieval provenance.

    ``is_fallback`` (with ``score == 0``) marks a candidate returned by the 0/97
    never-empty fallback rather than a genuine lexical overlap — the signal a caller
    uses to apply a confidence floor (e.g. the out-of-scope guard) so a manufactured
    candidate is never mistaken for a real match.
    """

    entry: CatalogEntry
    score: int
    is_fallback: bool


class Catalog:
    def __init__(
        self, operations: list[Operation], blurbs: Mapping[str, str] | None = None
    ):
        """``blurbs`` (optional) maps ``tool_name`` -> a pre-generated, already-sanitized
        S0 blurb folded into the overlap haystack. Absent -> the unchanged plain baseline."""
        b = blurbs or {}
        self.entries = [
            CatalogEntry(o, blurb=b.get(tool_name(o), "")) for o in operations
        ]

    def search_scored(self, query: str, limit: int = 5) -> list[ScoredEntry]:
        """Rank operations for ``query``; never empty for a query that carries intent.

        Genuine lexical hits (``score > 0``) rank first, exactly as before. When a
        MEANINGFUL query overlaps no operation's surface text — the shipped "0/97"
        discovery bug, where the op went invisible — it falls back to a non-semantic,
        query-independent prior (reads first, then path) rather than returning []. An
        empty/no-token query carries no intent and still yields [] (distinct case).
        """
        q = _tokens(query)
        if not q:
            return []
        scored = [(e.score(q), e) for e in self.entries]
        matches = sorted(
            (se for se in scored if se[0] > 0),
            key=lambda se: (-se[0], se[1].operation.path),
        )
        if matches:
            return [ScoredEntry(e, s, False) for s, e in matches[:limit]]
        # 0/97 fallback: deterministic, non-semantic, query-independent. Flagged
        # score-0 / is_fallback so it stays below any confidence floor.
        fallback = sorted(
            self.entries,
            key=lambda e: (0 if e.operation.method == "GET" else 1, e.operation.path),
        )
        return [ScoredEntry(e, 0, True) for e in fallback[:limit]]

    def search(self, query: str, limit: int = 5) -> list[CatalogEntry]:
        return [s.entry for s in self.search_scored(query, limit)]

    def by_tag(self) -> dict[str, list[CatalogEntry]]:
        grouped: dict[str, list[CatalogEntry]] = defaultdict(list)
        for e in self.entries:
            for tag in e.operation.tags or ["(untagged)"]:
                grouped[tag].append(e)
        return dict(grouped)

    def describe(self) -> str:
        """An agent/human-readable capability map, grouped by tag."""
        lines: list[str] = []
        for tag, entries in sorted(self.by_tag().items()):
            lines.append(f"## {tag}")
            for e in entries:
                o = e.operation
                lines.append(f"- {o.method} {o.path} — {o.summary}")
        return "\n".join(lines)
