"""``MongoStore``: the ``Store`` protocol on MongoDB, one database per customer.

The names and indexes are ``store_mongo.COLLECTIONS``; this module only uses them.
Each customer's database is ``db_name_for(customer)``, its indexes are ensured on the
first write per process, and ``delete_customer`` is one ``dropDatabase``. No query
names a database other than the caller's customer's, so nothing can be read across
customers.

The unique keys are the database's, not ours: a duplicate run, fact, charge or
signature is refused by the index and mapped to the same error ``MemoryStore`` raises.
Any other driver error becomes ``StoreUnavailableError`` with a message that never
carries the connection string.

The connection string comes from the OS keychain only (``mongo_store_from_keychain``),
through ``gecko.credentials``; never from ``.env``, never from a tool argument.
"""

from __future__ import annotations

import dataclasses
import re
import types
import typing
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

from gecko.credentials import (
    ChainResolver,
    CredentialError,
    CredentialRef,
    KeyringBackend,
)

from .store import (
    UNIQUE_LEDGER_KINDS,
    DuplicateChargeError,
    DuplicateDatasetError,
    DuplicateFactError,
    DuplicateRunError,
    StaleDatasetError,
    StoreError,
    StoreUnavailableError,
    check_dataset_rows,
    check_dataset_save,
    check_settle,
    same_question,
)
from .store_mongo import COLLECTIONS, db_name_for, ensure_indexes
from .types import (
    Bundle,
    Dataset,
    DatasetRow,
    FactRow,
    LedgerEntry,
    PriorValue,
    QueryPlan,
    Quote,
    RunRecord,
)

__all__ = [
    "MONGO_REF",
    "MongoStore",
    "connect_mongo",
    "mongo_store_from_keychain",
]

#: The keychain slot of the any-query connection string: ``MONGODB_URI``.
MONGO_REF = CredentialRef("MONGODB_URI")
_DUPLICATE = 11000
_INDEX_IN_MESSAGE = re.compile(r"index:\s*(\S+)")
_ID_FIELD = {spec.name: spec.id_field for spec in COLLECTIONS}
_GOOD = ["fresh", "repaired"]

T = TypeVar("T")


# --- documents <-> dataclasses ------------------------------------------------------


def _to_doc(collection: str, row: Any) -> dict[str, Any]:
    doc = asdict(row)
    id_field = _ID_FIELD[collection]
    if id_field is not None:
        doc["_id"] = doc[id_field]
    return doc


def _decode(hint: Any, value: Any) -> Any:
    """``value`` as read from BSON, back into ``hint``'s shape (lists -> tuples,
    dicts -> dataclasses). Literals and scalars pass through unchanged."""
    if value is None:
        return None
    origin = get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        inner = [a for a in get_args(hint) if a is not type(None)]
        return _decode(inner[0], value) if len(inner) == 1 else value
    if origin is tuple:
        args = get_args(hint)
        item = args[0] if args else Any
        return tuple(_decode(item, v) for v in value)
    if origin is dict:
        return dict(value)
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _from_doc(hint, value)
    return value


def _from_doc(cls: type[T], doc: Mapping[str, Any]) -> T:
    hints = get_type_hints(cls)
    kwargs = {
        f.name: _decode(hints[f.name], doc[f.name])
        for f in dataclasses.fields(cls)  # type: ignore[arg-type]
        if f.name in doc
    }
    return cls(**kwargs)


# --- errors ---------------------------------------------------------------------------


def _driver_errors() -> tuple[type[BaseException], ...]:
    try:
        from pymongo.errors import PyMongoError
    except ImportError:  # the [events]/[dense] extra is not installed
        return ()
    return (PyMongoError,)


def _duplicate_index(exc: BaseException) -> str | None:
    """The index a duplicate-key error names, or ``None`` if it is not one.

    Duck-typed on ``code == 11000`` so the offline fake and pymongo's
    ``DuplicateKeyError`` are read the same way."""
    if getattr(exc, "code", None) != _DUPLICATE:
        return None
    details = getattr(exc, "details", None) or {}
    message = str(details.get("errmsg") or "") if isinstance(details, dict) else ""
    found = _INDEX_IN_MESSAGE.search(message)
    return found.group(1) if found else ""


# --- the store ------------------------------------------------------------------------


@dataclass
class MongoStore:
    """``Store`` on a pymongo-shaped client: ``client[db][collection]``."""

    client: Any
    prefix: str = "gq_"
    #: Extra exception types read as "the store could not answer" (tests inject one).
    unavailable: tuple[type[BaseException], ...] = ()
    _indexed: set[str] = field(default_factory=set, repr=False)

    def _db(self, customer: str) -> Any:
        try:
            return self.client[db_name_for(customer, self.prefix)]
        except ValueError:
            raise StoreError("customer must be a non-empty account id") from None

    def _writable(self, customer: str) -> Any:
        db = self._db(customer)
        name = db_name_for(customer, self.prefix)
        if name not in self._indexed:
            with self._guard():
                ensure_indexes(db)
            self._indexed.add(name)
        return db

    @contextmanager
    def _guard(self) -> Iterator[None]:
        errors = (*_driver_errors(), *self.unavailable)
        try:
            yield
        except StoreError:
            raise
        except Exception as exc:
            if _duplicate_index(exc) is not None:
                raise
            if errors and isinstance(exc, errors):
                # The driver's message can carry the host; ours never does.
                raise StoreUnavailableError(
                    "the any-query store did not answer; nothing was changed"
                ) from None
            raise

    def _find(
        self,
        customer: str,
        collection: str,
        query: Mapping[str, Any],
        sort: Sequence[tuple[str, int]] = (),
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        with self._guard():
            cursor = self._db(customer)[collection].find(dict(query))
            if sort:
                cursor = cursor.sort(list(sort))
            if limit:
                cursor = cursor.limit(limit)
            return [dict(doc) for doc in cursor]

    def _one(self, customer: str, collection: str, key: str, cls: type[T]) -> T | None:
        found = self._find(customer, collection, {"_id": key}, limit=1)
        return _from_doc(cls, found[0]) if found else None

    # plans and quotes: written once, replaced only by the same id.

    def save_plan(self, plan: QueryPlan) -> None:
        self._replace(plan.customer, "plans", plan)

    def plan(self, customer: str, plan_id: str) -> QueryPlan | None:
        return self._one(customer, "plans", plan_id, QueryPlan)

    def plan_for(
        self, customer: str, query: str, subjects: Sequence[str]
    ) -> QueryPlan | None:
        # Filtered on the text in Mongo, on the subjects here: one customer asks one
        # question a handful of times, and the order of the subjects does not matter.
        for doc in self._find(
            customer, "plans", {"query": query}, sort=(("created_at", -1),)
        ):
            plan = _from_doc(QueryPlan, doc)
            if same_question(plan, query, subjects):
                return plan
        return None

    def save_bundle(self, bundle: Bundle) -> None:
        self._replace(bundle.customer, "bundles", bundle)

    def bundle(self, customer: str, name: str) -> Bundle | None:
        return self._one(customer, "bundles", name, Bundle)

    def bundles(self, customer: str) -> list[Bundle]:
        return [
            _from_doc(Bundle, doc)
            for doc in self._find(customer, "bundles", {}, sort=(("name", 1),))
        ]

    def save_quote(self, quote: Quote) -> None:
        self._replace(quote.customer, "quotes", quote)

    def quote(self, customer: str, quote_id: str) -> Quote | None:
        return self._one(customer, "quotes", quote_id, Quote)

    def _replace(self, customer: str, collection: str, row: Any) -> None:
        doc = _to_doc(collection, row)
        db = self._writable(customer)
        with self._guard():
            db[collection].replace_one({"_id": doc["_id"]}, doc, upsert=True)

    # runs

    def start_run(self, run: RunRecord) -> None:
        db = self._writable(run.customer)
        try:
            with self._guard():
                db["runs"].insert_one(_to_doc("runs", run))
        except Exception as exc:
            index = _duplicate_index(exc)
            if index is None:
                raise
            if index == "one_run_per_quote":
                raise DuplicateRunError(
                    f"a run already exists for quote {run.quote_id}"
                ) from None
            raise DuplicateRunError(f"run {run.run_id} already exists") from None

    def finish_run(self, run: RunRecord) -> None:
        db = self._writable(run.customer)
        doc = _to_doc("runs", run)
        with self._guard():
            held = db["runs"].find_one({"_id": run.run_id})
            if held is None:
                raise StoreError(f"run {run.run_id} was never started")
            if held.get("quote_id") != run.quote_id or held.get("plan_id") != (
                run.plan_id
            ):
                raise StoreError("a finished run cannot change its quote or plan")
            db["runs"].replace_one(
                {"_id": run.run_id, "quote_id": run.quote_id, "plan_id": run.plan_id},
                doc,
            )

    def run(self, customer: str, run_id: str) -> RunRecord | None:
        return self._one(customer, "runs", run_id, RunRecord)

    def runs(
        self, customer: str, *, plan_id: str | None = None, limit: int = 20
    ) -> list[RunRecord]:
        if limit <= 0:
            return []
        query = {} if plan_id is None else {"plan_id": plan_id}
        docs = self._find(customer, "runs", query, [("run_id", -1)], limit)
        return [_from_doc(RunRecord, d) for d in docs]

    # facts

    def write_facts(self, customer: str, rows: Sequence[FactRow]) -> None:
        batch: set[tuple[str, str, str]] = set()
        for row in rows:
            if row.customer != customer:
                raise StoreError("a fact row belongs to another customer")
            key = (row.run_id, row.subject, row.field)
            if key in batch:
                raise DuplicateFactError(
                    f"fact ({row.run_id}, {row.subject!r}, {row.field}) already exists"
                )
            batch.add(key)
        if not rows:
            return
        db = self._writable(customer)
        # Checked before the insert so a refused batch writes nothing, as in memory.
        # The unique index still holds the line against a concurrent writer.
        for run_id in sorted({r.run_id for r in rows}):
            with self._guard():
                held = db["facts"].find({"run_id": run_id})
                taken = {(run_id, d.get("subject"), d.get("field")) for d in held}
            clash = sorted(taken & batch)
            if clash:
                _rid, subject, name = clash[0]
                raise DuplicateFactError(
                    f"fact ({run_id}, {subject!r}, {name}) already exists"
                )
        try:
            with self._guard():
                db["facts"].insert_many(
                    [_to_doc("facts", r) for r in rows], ordered=True
                )
        except Exception as exc:
            if _duplicate_index(exc) is None:
                raise
            raise DuplicateFactError("a fact of this batch already exists") from None

    def facts(
        self,
        customer: str,
        *,
        run_id: str,
        subject: str | None = None,
        field: str | None = None,
    ) -> list[FactRow]:
        query: dict[str, Any] = {"run_id": run_id}
        if subject is not None:
            query["subject"] = subject
        if field is not None:
            query["field"] = field
        docs = self._find(customer, "facts", query, _CELL_ORDER)
        return [_from_doc(FactRow, d) for d in docs]

    def plan_facts(self, customer: str, plan_id: str) -> list[dict[str, Any]]:
        docs = self._find(customer, "facts", {"plan_id": plan_id}, _CELL_ORDER)
        return [asdict(_from_doc(FactRow, d)) for d in docs]

    def last_good(
        self, customer: str, plan_id: str
    ) -> dict[tuple[str, str], PriorValue]:
        query = {"plan_id": plan_id, "status": {"$in": _GOOD}}
        out: dict[tuple[str, str], PriorValue] = {}
        for doc in self._find(customer, "facts", query, _CELL_ORDER):
            out[(doc["subject"], doc["field"])] = PriorValue(
                doc["run_id"], doc.get("value"), doc["status"]
            )
        return out

    # calls: names and counts, never values

    def append_calls(self, customer: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        db = self._writable(customer)
        # Copies without any _id: Mongo assigns one, and the caller's dicts stay clean.
        docs = [{k: v for k, v in dict(r).items() if k != "_id"} for r in rows]
        with self._guard():
            db["calls"].insert_many(docs, ordered=True)

    def calls(
        self, customer: str, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = {} if run_id is None else {"run_id": run_id}
        docs = self._find(customer, "calls", query, [("_id", 1)])
        return [{k: v for k, v in d.items() if k != "_id"} for d in docs]

    # payments ledger

    def append_ledger(self, entry: LedgerEntry) -> None:
        if entry.amount_usdc_micro < 0:
            raise StoreError("ledger amounts are never negative; kind gives direction")
        db = self._writable(entry.customer)
        try:
            with self._guard():
                db["payments"].insert_one(_to_doc("payments", entry))
        except Exception as exc:
            index = _duplicate_index(exc)
            if index is None:
                raise
            if (
                index in ("one_charge_per_run", "one_credit_per_run")
                and entry.kind in UNIQUE_LEDGER_KINDS
            ):
                raise DuplicateChargeError(
                    f"a {entry.kind} already exists for run {entry.run_id}"
                ) from None
            if index == "one_open_settlement_per_run":
                raise DuplicateChargeError(
                    f"a settlement is already open for run {entry.run_id}"
                ) from None
            if index == "one_entry_per_signature":
                raise DuplicateChargeError(
                    "this signature is already in the ledger"
                ) from None
            raise StoreError(f"ledger entry {entry.entry_id} already exists") from None

    def update_ledger(self, entry: LedgerEntry) -> None:
        db = self._writable(entry.customer)
        check_settle(None, entry)
        settle = {
            "status": entry.status,
            "signature": entry.signature,
            "slot": entry.slot,
            "sol_usdc_rate_micro": entry.sol_usdc_rate_micro,
            "note": entry.note,
        }
        try:
            with self._guard():
                held = db["payments"].find_one({"_id": entry.entry_id})
                if held is None:
                    raise StoreError(f"ledger entry {entry.entry_id} does not exist")
                check_settle(held.get("signature"), entry)
                # The status and signature in the filter make the move atomic: two
                # settles of one pending entry cannot both match, and a recorded
                # signature cannot be swapped between the read and the write.
                result = db["payments"].update_one(
                    {
                        "_id": entry.entry_id,
                        "status": "pending",
                        "signature": {"$in": [None, entry.signature]},
                    },
                    {"$set": settle},
                )
        except Exception as exc:
            if _duplicate_index(exc) is None:
                raise
            raise DuplicateChargeError(
                "this signature is already in the ledger"
            ) from None
        if getattr(result, "matched_count", 0) != 1:
            raise StoreError(
                "a ledger entry only moves from pending to landed or failed"
            )

    def ledger(self, customer: str, *, limit: int = 50) -> list[LedgerEntry]:
        if limit <= 0:
            return []
        order = [("created_at", -1), ("entry_id", -1)]
        docs = self._find(customer, "payments", {}, order, limit)
        return [_from_doc(LedgerEntry, d) for d in docs]

    # datasets: the customer's living tables, and one row document per subject

    def create_dataset(self, dataset: Dataset) -> None:
        db = self._writable(dataset.customer)
        try:
            with self._guard():
                db["datasets"].insert_one(_to_doc("datasets", dataset))
        except Exception as exc:
            if _duplicate_index(exc) is None:
                raise
            raise DuplicateDatasetError(
                f"dataset {dataset.dataset_id} already exists"
            ) from None

    def save_dataset(self, dataset: Dataset, *, expect_revision: int) -> None:
        check_dataset_save(dataset, expect_revision)
        db = self._writable(dataset.customer)
        with self._guard():
            # The revision in the filter is the lock: two writers that read the same
            # revision cannot both match.
            result = db["datasets"].replace_one(
                {"_id": dataset.dataset_id, "revision": expect_revision},
                _to_doc("datasets", dataset),
            )
        if getattr(result, "matched_count", 0) != 1:
            raise StaleDatasetError(
                f"dataset {dataset.dataset_id} is not at revision {expect_revision}"
            )

    def dataset(self, customer: str, dataset_id: str) -> Dataset | None:
        return self._one(customer, "datasets", dataset_id, Dataset)

    def datasets(self, customer: str) -> list[Dataset]:
        docs = self._find(customer, "datasets", {}, sort=(("dataset_id", 1),))
        return [_from_doc(Dataset, d) for d in docs]

    def write_dataset_rows(
        self, customer: str, dataset_id: str, rows: Sequence[DatasetRow]
    ) -> None:
        check_dataset_rows(customer, dataset_id, rows)
        if not rows:
            return
        db = self._writable(customer)
        for row in rows:
            doc = _to_doc("dataset_rows", row)
            with self._guard():
                db["dataset_rows"].replace_one({"_id": doc["_id"]}, doc, upsert=True)

    def remove_dataset_rows(
        self, customer: str, dataset_id: str, subjects: Sequence[str]
    ) -> None:
        if not subjects:
            return
        db = self._writable(customer)
        with self._guard():
            db["dataset_rows"].delete_many(
                {"dataset_id": dataset_id, "subject": {"$in": list(subjects)}}
            )

    def dataset_rows(
        self,
        customer: str,
        dataset_id: str,
        *,
        subject: str | None = None,
        limit: int = 100,
    ) -> list[DatasetRow]:
        if limit <= 0:
            return []
        query: dict[str, Any] = {"dataset_id": dataset_id}
        if subject is not None:
            query["subject"] = subject
        docs = self._find(customer, "dataset_rows", query, [("subject", 1)], limit)
        return [_from_doc(DatasetRow, d) for d in docs]

    def delete_dataset(self, customer: str, dataset_id: str) -> None:
        db = self._writable(customer)
        with self._guard():
            db["dataset_rows"].delete_many({"dataset_id": dataset_id})
            db["datasets"].delete_one({"_id": dataset_id})

    def delete_customer(self, customer: str) -> None:
        try:
            name = db_name_for(customer, self.prefix)
        except ValueError:
            raise StoreError("customer must be a non-empty account id") from None
        with self._guard():
            self.client.drop_database(name)
        self._indexed.discard(name)


_CELL_ORDER = [("run_id", 1), ("subject", 1), ("field", 1)]


# --- connecting -----------------------------------------------------------------------


def connect_mongo(uri: str) -> Any:  # pragma: no cover - wire
    """A pymongo client that returns timezone-aware times and gives up fast."""
    try:
        from pymongo import MongoClient
    except ImportError:
        raise StoreUnavailableError(
            "pymongo is not installed; install the [events] extra"
        ) from None
    return MongoClient(uri, serverSelectionTimeoutMS=2000, tz_aware=True)


def mongo_store_from_keychain(
    *,
    resolver: ChainResolver | None = None,
    connect: Callable[[str], Any] = connect_mongo,
    ref: CredentialRef = MONGO_REF,
) -> MongoStore:
    """The store, with its connection string read from the OS keychain only.

    The default resolver has the keyring backend and nothing else: no env fallback,
    so a connection string never comes from ``.env``. Every error names the slot,
    never the value.
    """
    chain = resolver if resolver is not None else ChainResolver([KeyringBackend()])
    try:
        uri = chain.resolve(ref)
    except CredentialError:
        raise StoreUnavailableError(
            f"no MongoDB connection string in the keychain slot {ref.slot()!r}; "
            f"set it with: gecko auth set {ref.api}"
        ) from None
    try:
        client = connect(uri)
    except StoreError:
        raise
    except Exception:
        raise StoreUnavailableError(
            f"could not open the MongoDB client for {ref.slot()!r}"
        ) from None
    return MongoStore(client)
