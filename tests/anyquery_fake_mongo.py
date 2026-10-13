"""An in-memory stand-in for the slice of pymongo ``MongoStore`` uses.

mongomock is not a dependency, so this fake enforces what matters for the store's
contract the way the server does: ``_id`` and every ``unique`` index (with its
``partialFilterExpression``) raise a duplicate-key error carrying ``code == 11000`` and
the index name in ``errmsg``; a database exists only after a write; ``insert_many``
with ``ordered=True`` keeps the documents before a duplicate. Filters support equality,
``$in``, ``$lte`` and ``$type: "string"``, which is all the store and the indexes use.
``find_one_and_update`` with ``$inc`` runs under one lock, as the server runs a
single-document update: a filter and its increment cannot be split by another caller.
"""

from __future__ import annotations

import copy
import itertools
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any


class FakeDuplicateKeyError(Exception):
    def __init__(self, index: str) -> None:
        self.code = 11000
        self.details = {"errmsg": f"E11000 duplicate key error index: {index} dup key"}
        super().__init__(self.details["errmsg"])


class FakeUnavailableError(Exception):
    """Stands in for a pymongo ``ServerSelectionTimeoutError``; carries the URI."""


def _matches_one(value: Any, cond: Any) -> bool:
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$in" and value not in arg:
                return False
            if op == "$type" and not (arg == "string" and isinstance(value, str)):
                return False
            if op == "$lte" and not (
                isinstance(value, int) and not isinstance(value, bool) and value <= arg
            ):
                return False
            if op not in ("$in", "$type", "$lte"):
                raise NotImplementedError(op)
        return True
    return value == cond


def matches(doc: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
    return all(_matches_one(doc.get(k), v) for k, v in query.items())


@dataclass
class _Result:
    matched_count: int


@dataclass
class _Deleted:
    deleted_count: int


@dataclass
class _Index:
    keys: list[tuple[str, int]]
    unique: bool
    partial: dict[str, Any] | None


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def sort(self, keys: list[tuple[str, int]]) -> FakeCursor:
        for key, direction in reversed(keys):
            self._docs.sort(
                key=lambda d, k=key: (d.get(k) is not None, d.get(k)),
                reverse=direction < 0,
            )
        return self

    def limit(self, n: int) -> FakeCursor:
        self._docs = self._docs[:n]
        return self

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(copy.deepcopy(self._docs))


@dataclass
class FakeCollection:
    client: FakeMongoClient
    db_name: str
    docs: list[dict[str, Any]] = field(default_factory=list)
    indexes: dict[str, _Index] = field(default_factory=dict)

    def _touch(self) -> None:
        self.client.client_ops += 1
        if self.client.down:
            raise FakeUnavailableError(f"cannot reach {self.client.uri}")

    def create_index(self, keys: list[tuple[str, int]], **options: Any) -> str:
        self._touch()
        self.client.created.add(self.db_name)
        self.indexes[options["name"]] = _Index(
            list(keys),
            bool(options.get("unique")),
            options.get("partialFilterExpression"),
        )
        return str(options["name"])

    def _check_unique(self, doc: dict[str, Any], skip: Any = None) -> None:
        others = [d for d in self.docs if d is not skip]
        if any(d["_id"] == doc["_id"] for d in others):
            raise FakeDuplicateKeyError("_id_")
        for name, index in self.indexes.items():
            if not index.unique:
                continue
            if index.partial is not None and not matches(doc, index.partial):
                continue
            key = tuple(doc.get(k) for k, _ in index.keys)
            for other in others:
                if index.partial is not None and not matches(other, index.partial):
                    continue
                if tuple(other.get(k) for k, _ in index.keys) == key:
                    raise FakeDuplicateKeyError(name)

    def insert_one(self, doc: Mapping[str, Any]) -> None:
        with self.client.lock:
            self._touch()
            stored = copy.deepcopy(dict(doc))
            stored.setdefault("_id", next(self.client.ids))
            self._check_unique(stored)
            self.client.created.add(self.db_name)
            self.docs.append(stored)

    def insert_many(self, docs: list[Mapping[str, Any]], ordered: bool = True) -> None:
        assert ordered
        for doc in docs:
            self.insert_one(doc)

    def find(self, query: Mapping[str, Any] | None = None) -> FakeCursor:
        self._touch()
        return FakeCursor([d for d in self.docs if matches(d, query or {})])

    def find_one(self, query: Mapping[str, Any]) -> dict[str, Any] | None:
        found = list(self.find(query).limit(1))
        return found[0] if found else None

    def replace_one(
        self, query: Mapping[str, Any], doc: Mapping[str, Any], upsert: bool = False
    ) -> _Result:
        self._touch()
        for i, held in enumerate(self.docs):
            if matches(held, query):
                new = {**copy.deepcopy(dict(doc)), "_id": held["_id"]}
                self._check_unique(new, skip=held)
                self.docs[i] = new
                return _Result(1)
        if upsert:
            self.insert_one(doc)
        return _Result(0)

    def delete_one(self, query: Mapping[str, Any]) -> _Deleted:
        with self.client.lock:
            self._touch()
            for i, held in enumerate(self.docs):
                if matches(held, query):
                    del self.docs[i]
                    return _Deleted(1)
            return _Deleted(0)

    def delete_many(self, query: Mapping[str, Any]) -> _Deleted:
        with self.client.lock:
            self._touch()
            kept = [d for d in self.docs if not matches(d, query)]
            gone = len(self.docs) - len(kept)
            self.docs[:] = kept
            return _Deleted(gone)

    def update_one(
        self, query: Mapping[str, Any], update: Mapping[str, Any]
    ) -> _Result:
        self._touch()
        for i, held in enumerate(self.docs):
            if matches(held, query):
                new = {**held, **copy.deepcopy(dict(update["$set"]))}
                self._check_unique(new, skip=held)
                self.docs[i] = new
                return _Result(1)
        return _Result(0)

    def find_one_and_update(
        self,
        query: Mapping[str, Any],
        update: Mapping[str, Any],
        upsert: bool = False,
    ) -> dict[str, Any] | None:
        """Only ``$inc``, as the spend ledger uses it; returns the document BEFORE."""
        if set(update) != {"$inc"}:
            raise NotImplementedError(sorted(update))
        with self.client.lock:
            self._touch()
            for i, held in enumerate(self.docs):
                if matches(held, query):
                    new = dict(held)
                    for key, delta in update["$inc"].items():
                        new[key] = new.get(key, 0) + delta
                    self.docs[i] = new
                    return copy.deepcopy(held)
            if upsert:
                fresh = {k: v for k, v in query.items() if not isinstance(v, dict)}
                fresh.update(update["$inc"])
                self.insert_one(fresh)
            return None


@dataclass
class FakeDb:
    client: FakeMongoClient
    name: str
    collections: dict[str, FakeCollection] = field(default_factory=dict)

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection(self.client, self.name))


@dataclass
class FakeMongoClient:
    uri: str = "mongodb+srv://user:hunter2@cluster.example.net/"
    down: bool = False
    dbs: dict[str, FakeDb] = field(default_factory=dict)
    created: set[str] = field(default_factory=set)
    dropped: list[str] = field(default_factory=list)
    client_ops: int = 0
    ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def __getitem__(self, name: str) -> FakeDb:
        return self.dbs.setdefault(name, FakeDb(self, name))

    def drop_database(self, name: str) -> None:
        if self.down:
            raise FakeUnavailableError(f"cannot reach {self.uri}")
        self.dbs.pop(name, None)
        self.created.discard(name)
        self.dropped.append(name)

    def database_names(self) -> set[str]:
        """Databases that exist on the server: written to at least once."""
        return set(self.created)
