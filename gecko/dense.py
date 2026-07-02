"""The dense retrieval arm — MongoDB Atlas ``$vectorSearch`` over per-op ``SurfaceDoc``s.

``DenseIndex`` is the injected seam (the second adapter seam, next to ``Session.auth_headers``
and ``Enricher``): query text -> ranked ``(tool_name, score)`` for ONE surface. The concrete
``MongoAtlasDenseIndex`` bakes ``surface_id`` in and applies it as a ``$vectorSearch`` FILTER
path — a per-*API* pre-filter, not per-request — so fusion in ``client`` stays provider-free
(the pymongo SDK never leaves this module; invariant #2).

Embedding is native Atlas ``autoEmbed`` (Voyage ``voyage-4-lite``): the index embeds
``embed_text`` server-side on write and embeds the query text on read, so no embedding key
lives in this process (the founder's MongoDB credits). Swapping to pre-computed vectors is a
different ``DenseIndex`` implementation behind the same seam — the choice stays injected.

Store discipline (§5): a materialized VIEW keyed ``(surface_id, operation_id)`` — upsert
REPLACES (never appends, which would double-count in fusion); a doc whose ``embed_text_hash``
is unchanged is skipped so a spec edit re-embeds only what actually changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .surfacedoc import SurfaceDoc

DB_NAME = "gecko_rag"
COLLECTION = "surface_docs"
INDEX_NAME = "surface_vec"
EMBED_MODEL = "voyage-4-lite"
EMBED_PATH = "embed_text"
FILTER_PATH = "surface_id"


class DenseIndex(Protocol):
    """query text -> ranked ``(tool_name, score)`` for the bound surface. The injected seam
    fusion sits behind; a deterministic fake in tests implements the same two-method shape."""

    def search(self, query: str, limit: int) -> list[tuple[str, float]]: ...


def vector_index_definition() -> dict[str, Any]:
    """The Atlas ``$vectorSearch`` autoEmbed index: ``embed_text`` embedded natively by
    Voyage, ``surface_id`` a filter path for the per-API pre-filter."""
    return {
        "fields": [
            {
                "type": "autoEmbed",
                "path": EMBED_PATH,
                "model": EMBED_MODEL,
                "modality": "text",
            },
            {"type": "filter", "path": FILTER_PATH},
        ]
    }


def _doc_to_record(doc: SurfaceDoc) -> dict[str, Any]:
    """The stored Mongo document — surface text only (invariant #1), plus the autoEmbed
    ``embed_text`` path Atlas embeds server-side. No response payload, no value, no token."""
    return {
        "surface_id": doc.surface_id,
        "operation_id": doc.operation_id,
        "content": doc.content,
        "contextualized_content": doc.contextualized_content,
        "embed_text": doc.embed_text,
        "embed_text_hash": doc.embed_text_hash,
    }


class MongoAtlasDenseIndex:
    """Atlas-backed ``DenseIndex`` for one ``surface_id`` (native autoEmbed)."""

    def __init__(
        self,
        uri: str,
        surface_id: str,
        *,
        db: str = DB_NAME,
        collection: str = COLLECTION,
        index_name: str = INDEX_NAME,
        num_candidates: int = 100,
    ):
        import certifi
        from pymongo import MongoClient

        self._client: Any = MongoClient(
            uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=20000
        )
        self._coll = self._client[db][collection]
        self.surface_id = surface_id
        self.index_name = index_name
        self.num_candidates = num_candidates

    # --- write side (materialized view) ------------------------------------------------

    def ensure_index(self) -> None:
        """Create the autoEmbed vector index if absent (idempotent). The collection must
        exist before a search index can be created, so materialize it first."""
        from pymongo.errors import CollectionInvalid

        db = self._coll.database
        if self._coll.name not in db.list_collection_names():
            try:
                db.create_collection(self._coll.name)
            except CollectionInvalid:
                pass  # created concurrently — fine
        existing = {ix.get("name") for ix in self._coll.list_search_indexes()}
        if self.index_name in existing:
            return
        from pymongo.operations import SearchIndexModel

        self._coll.create_search_index(
            SearchIndexModel(
                definition=vector_index_definition(),
                name=self.index_name,
                type="vectorSearch",
            )
        )

    def index_ready(self) -> bool:
        for ix in self._coll.list_search_indexes():
            if ix.get("name") == self.index_name:
                return bool(ix.get("queryable"))
        return False

    def upsert(self, docs: Sequence[SurfaceDoc]) -> dict[str, int]:
        """Replace docs keyed ``(surface_id, operation_id)`` — a materialized view, never
        append-only. A doc whose ``embed_text_hash`` matches the stored one is skipped so
        Atlas does not needlessly re-embed unchanged surface text. Returns write counts."""
        written = skipped = 0
        for doc in docs:
            key = {"surface_id": doc.surface_id, "operation_id": doc.operation_id}
            prior = self._coll.find_one(key, {"embed_text_hash": 1})
            if prior and prior.get("embed_text_hash") == doc.embed_text_hash:
                skipped += 1
                continue
            self._coll.replace_one(key, _doc_to_record(doc), upsert=True)
            written += 1
        return {"written": written, "skipped": skipped}

    # --- read side ---------------------------------------------------------------------

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """Text ``$vectorSearch`` (autoEmbed embeds the query), pre-filtered to this
        surface. Returns ranked ``(operation_id/tool_name, cosine score)``."""
        pipeline = [
            {
                "$vectorSearch": {
                    "index": self.index_name,
                    "path": EMBED_PATH,
                    "query": query,
                    "numCandidates": max(self.num_candidates, limit * 4),
                    "limit": limit,
                    "filter": {FILTER_PATH: self.surface_id},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "operation_id": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        return [
            (str(r["operation_id"]), float(r["score"]))
            for r in self._coll.aggregate(pipeline)
        ]

    def close(self) -> None:
        self._client.close()
