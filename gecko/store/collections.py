"""A minimal duck-typed collection seam — in-memory for tests, Mongo in prod.

Deliberately tiny: the store layer needs only insert, find (with an equality
filter), and count. Modeling exactly that keeps the engine free of a hard
pymongo dependency and lets every store test run with no database. The Mongo
adapter (a thin wrapper whose ``insert_one``/``find``/``count_documents`` match
this shape) lands as its own follow-up; nothing here imports it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Collection(Protocol):
    """The subset of a document collection the store layer uses."""

    def insert_one(self, document: Mapping[str, Any]) -> None: ...

    def find(self, query: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
        """Every document matching an equality ``query`` (all keys must match)."""
        ...

    def count_documents(self, query: Mapping[str, Any]) -> int: ...


class InMemoryCollection:
    """A list-backed :class:`Collection` for tests. Equality-only matching.

    Stores copies, not references, so a caller mutating an inserted mapping
    cannot reach back into the collection — the same isolation the wire gives.
    """

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def insert_one(self, document: Mapping[str, Any]) -> None:
        self._docs.append(dict(document))

    @staticmethod
    def _matches(doc: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
        return all(doc.get(key) == value for key, value in query.items())

    def find(self, query: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
        return (dict(doc) for doc in self._docs if self._matches(doc, query))

    def count_documents(self, query: Mapping[str, Any]) -> int:
        return sum(1 for doc in self._docs if self._matches(doc, query))
