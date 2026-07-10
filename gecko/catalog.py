"""Lightweight capability catalog — the "find the right endpoint" layer.

Lexical search over the operations' surface text. At ~tens of endpoints this is
more accurate and far simpler than vector RAG; vectorization is the multi-API /
large-API play (V2), deliberately deferred.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .ingest import Operation
from .tools import tool_name

_WORD = re.compile(r"[a-z0-9]+")
# Sub-words INSIDE an identifier, split on camelCase / letter-digit / separator
# boundaries: `getApiOddsSnapshotFixtureid` -> get·Api·Odds·Snapshot·Fixtureid,
# `Epochday2` -> Epochday·2. Runs on the RAW (pre-lowercase) text so the camelCase
# boundary survives — the plain `[a-z0-9]+` pass lowercases first and loses it.
_IDENT_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _token_list(text: str) -> list[str]:
    """Ordered tokens WITH multiplicity via the identifier tokenizer (camelCase/digit/
    separator split), lowercased. Unlike ``_tokens`` (a set for overlap) this keeps term
    frequency — BM25 needs TF — and splits identifiers so `getApiOdds` -> get·api·odds."""
    return [part.lower() for part in _IDENT_PART.findall(text or "")]


def _tokens(text: str) -> set[str]:
    """Index/query tokens for lexical overlap.

    A STRICT SUPERSET of the old `[a-z0-9]+` lowercase pass: it keeps every original
    token AND adds the camelCase/digit-boundary sub-words of any identifier (the
    operationId in particular). This can only ADD recall — a query token like "odds"
    now matches `getApiOddsSnapshotFixtureid`, which the glued mega-token dropped — and
    never removes a match that used to work (see the superset guard test).
    """
    raw = text or ""
    tokens = set(_WORD.findall(raw.lower()))
    tokens.update(part.lower() for part in _IDENT_PART.findall(raw))
    return tokens


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


# --- Okapi BM25 (BM25F) — the SELECTABLE lexical arm (retrieval spec §1a, §4 arm A) --------
#
# A genuinely stronger lexical ranker than the overlap count: IDF (down-weights ubiquitous
# terms), TF-saturation (2nd occurrence adds less), length-norm (a verbose field no longer
# wins on token count), and per-field weights. Not wired into `Catalog.search`/`client` —
# it is built + measured against the overlap baseline (`scripts/retrieval_arms_eval.py`) and
# only adopted into the live path when the op-count gate (>50) fires.

BM25_K1: float = 1.5  # TF-saturation (Robertson default)
BM25_B: float = 0.75  # length-normalization strength

# OpenAPI-remapped field weights. chub weights `id: 4` highest because its entry id is a
# curated `author/name` slug — the most intent-bearing field. Our operationIds are
# AUTO-GENERATED (`getApiOddsSnapshotFixtureid`), so that prior is INVERTED: summary/tags/
# description carry intent (high), the operationId is low-signal (low but non-zero, so a
# camelCase-split identifier still contributes recall when the summary is thin).
BM25_FIELD_WEIGHTS: dict[str, float] = {
    "summary": 3.0,
    "tags": 2.5,
    "description": 2.0,
    "blurb": 2.0,
    "path": 1.5,
    "operation_id": 0.5,
}


def _entry_fields(entry: CatalogEntry) -> dict[str, str]:
    """The per-field text an OpenAPI op contributes to the BM25 haystack (weighted separately,
    unlike the overlap scorer's single glued haystack)."""
    o = entry.operation
    return {
        "summary": o.summary,
        "description": o.description,
        "path": o.path,
        "tags": " ".join(o.tags),
        "operation_id": o.operation_id,
        "blurb": entry.blurb,
    }


@dataclass(frozen=True)
class BM25Hit:
    """A BM25-ranked catalog hit. Mirrors ``ScoredEntry`` but carries a FLOAT score (BM25 is
    real-valued) and exposes ``name`` so it drops straight into the recall harness. A genuine
    hit has ``score > 0``; the never-empty fallback is flagged ``is_fallback`` at ``score 0``."""

    entry: CatalogEntry
    score: float
    is_fallback: bool

    @property
    def name(self) -> str:
        return self.entry.tool_name


class BM25Index:
    """Okapi BM25F over the catalog's per-field surface text — a pre-built inverted index.

    Separate from ``Catalog`` on purpose (invariant: the live overlap path is untouched until
    a measured win). Construct once, query many times; scoring only visits docs that share a
    query term (postings), so it is cheap even at hundreds of ops.
    """

    def __init__(
        self,
        entries: Sequence[CatalogEntry],
        *,
        field_weights: Mapping[str, float] | None = None,
        k1: float = BM25_K1,
        b: float = BM25_B,
    ):
        self.entries: list[CatalogEntry] = list(entries)
        self.weights = dict(field_weights or BM25_FIELD_WEIGHTS)
        self.k1 = k1
        self.b = b
        self._fields = list(self.weights)

        n = len(self.entries)
        # Per doc: {field -> Counter(term)} and {field -> length}. Postings: term -> doc idxs.
        self._counts: list[dict[str, Counter[str]]] = []
        self._lens: list[dict[str, int]] = []
        self._postings: dict[str, set[int]] = defaultdict(set)
        df: Counter[str] = Counter()
        field_len_sum: dict[str, int] = dict.fromkeys(self._fields, 0)

        fields_by_entry = [_entry_fields(e) for e in self.entries]
        for idx, fields in enumerate(fields_by_entry):
            doc_counts: dict[str, Counter[str]] = {}
            doc_lens: dict[str, int] = {}
            doc_terms: set[str] = set()
            for f in self._fields:
                toks = _token_list(fields.get(f, ""))
                c = Counter(toks)
                doc_counts[f] = c
                doc_lens[f] = len(toks)
                field_len_sum[f] += len(toks)
                for term in c:
                    self._postings[term].add(idx)
                doc_terms |= set(c)
            for term in doc_terms:  # df = docs containing the term in ANY field
                df[term] += 1
            self._counts.append(doc_counts)
            self._lens.append(doc_lens)

        # Lucene-style IDF: log(1 + (N - df + 0.5)/(df + 0.5)) — always positive, so a term
        # in every doc contributes ~0 rather than a negative that could flip a ranking.
        self._idf: dict[str, float] = {
            term: math.log(1.0 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()
        }
        self._avg_len: dict[str, float] = {
            f: (field_len_sum[f] / n if n else 0.0) for f in self._fields
        }

    def _score_doc(self, idx: int, query_terms: set[str]) -> float:
        score = 0.0
        counts = self._counts[idx]
        lens = self._lens[idx]
        for term in query_terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            weighted_tf = 0.0
            for f in self._fields:
                tf = counts[f].get(term, 0)
                if not tf:
                    continue
                avg = self._avg_len[f] or 1.0
                norm = 1.0 - self.b + self.b * (lens[f] / avg)
                weighted_tf += self.weights[f] * tf / norm
            if weighted_tf > 0.0:
                # BM25F saturation over the field-combined weighted TF.
                score += idf * weighted_tf / (self.k1 + weighted_tf)
        return score

    def search_scored(self, query: str, limit: int = 5) -> list[BM25Hit]:
        """Rank ops for ``query`` by BM25F; never empty for a query that carries intent.

        Genuine hits (``score > 0``) rank first (score desc, then path for stable ties). With
        no genuine hit — the same 0/97 case the overlap scorer guards — it falls back to the
        query-independent prior (GET-first, then path) flagged ``is_fallback`` at score 0, so
        the out-of-scope confidence floor reads identically to the overlap arm. An empty /
        no-token query carries no intent and yields ``[]``."""
        q = set(_token_list(query))
        if not q:
            return []
        candidates = (
            set().union(*(self._postings.get(t, set()) for t in q)) if q else set()
        )
        scored = [(self._score_doc(i, q), self.entries[i]) for i in candidates]
        matches = sorted(
            (se for se in scored if se[0] > 0.0),
            key=lambda se: (-se[0], se[1].operation.path),
        )
        if matches:
            return [BM25Hit(e, s, False) for s, e in matches[:limit]]
        fallback = sorted(
            self.entries,
            key=lambda e: (0 if e.operation.method == "GET" else 1, e.operation.path),
        )
        return [BM25Hit(e, 0.0, True) for e in fallback[:limit]]
