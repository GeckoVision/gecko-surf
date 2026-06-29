"""Lightweight capability catalog — the "find the right endpoint" layer.

Lexical search over the operations' surface text. At ~tens of endpoints this is
more accurate and far simpler than vector RAG; vectorization is the multi-API /
large-API play (V2), deliberately deferred.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .ingest import Operation

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


@dataclass
class CatalogEntry:
    operation: Operation

    @property
    def tool_name(self) -> str:
        return self.operation.operation_id

    @property
    def _haystack(self) -> str:
        o = self.operation
        return " ".join(
            [o.summary, o.description, o.path, " ".join(o.tags), o.operation_id]
        )

    def score(self, query_tokens: set[str]) -> int:
        if not query_tokens:
            return 0
        hay = _tokens(self._haystack)
        summary = _tokens(self.operation.summary)
        # summary matches count double (the most intent-bearing field)
        return len(query_tokens & hay) + len(query_tokens & summary)


class Catalog:
    def __init__(self, operations: list[Operation]):
        self.entries = [CatalogEntry(o) for o in operations]

    def search(self, query: str, limit: int = 5) -> list[CatalogEntry]:
        q = _tokens(query)
        scored = [(e.score(q), e) for e in self.entries]
        ranked = sorted(
            (se for se in scored if se[0] > 0),
            key=lambda se: (-se[0], se[1].operation.path),
        )
        return [e for _, e in ranked[:limit]]

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
