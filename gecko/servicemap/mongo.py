"""``MongoRegistry``: the registry contract on ``gecko_registry``.

A put reads the held row, merges (:mod:`gecko.servicemap.merge`) and replaces it. That
read-then-write is not atomic; it is safe for the one founder-run importer it serves
today, and a concurrent writer would need ``$max``-style updates instead. Events are
only ever inserted. Any driver error becomes :class:`RegistryUnavailableError`, whose
message never carries the host or the connection string.

The client comes from the caller (``scripts/servicemap_import.py`` reads the connection
string from the keychain); this module never resolves a credential.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar

# One decoder for every dataclass read back from BSON, shared with the any-query store.
from gecko.anyquery.mongo import _from_doc

from .ids import equivalence_id, field_id
from .merge import (
    merge_equivalence,
    merge_field,
    merge_offer,
    merge_operation,
    merge_service,
    merge_sighting,
)
from .registry import RegistryKilledError, RegistryUnavailableError
from .store_mongo import CONTROL, DB_NAME, EVENTS, KILL_SWITCH_ID, ensure_indexes
from .types import (
    Catalog,
    Equivalence,
    Event,
    FieldObs,
    Health,
    OfferObs,
    Operation,
    Service,
    Sighting,
)

__all__ = ["MongoRegistry"]

T = TypeVar("T")


def _driver_errors() -> tuple[type[BaseException], ...]:
    try:
        from pymongo.errors import PyMongoError
    except ImportError:
        return ()
    return (PyMongoError,)


@dataclass
class MongoRegistry:
    client: Any
    db_name: str = DB_NAME
    #: Extra exception types read as "the store could not answer" (tests inject one).
    unavailable: tuple[type[BaseException], ...] = ()
    _indexed: bool = field(default=False, repr=False)

    @property
    def _db(self) -> Any:
        return self.client[self.db_name]

    @contextmanager
    def _guard(self) -> Iterator[None]:
        errors = (*_driver_errors(), *self.unavailable)
        try:
            yield
        except Exception as exc:
            if errors and isinstance(exc, errors):
                raise RegistryUnavailableError(
                    "the service registry did not answer; nothing more was written"
                ) from None
            raise

    def killed(self) -> bool:
        with self._guard():
            doc = self._db[CONTROL].find_one({"_id": KILL_SWITCH_ID})
        return bool(doc and doc.get("engaged"))

    def _writable(self) -> Any:
        if self.killed():
            raise RegistryKilledError("the registry kill switch is engaged")
        db = self._db
        if not self._indexed:
            with self._guard():
                ensure_indexes(db)
            self._indexed = True
        return db

    def _merge_put(
        self,
        collection: str,
        key: str,
        row: T,
        merge: Callable[[T | None, T], T],
        id_field: str,
    ) -> None:
        db = self._writable()
        with self._guard():
            held_doc = db[collection].find_one({"_id": key})
            held = _from_doc(type(row), held_doc) if held_doc else None
            # T is any registry dataclass; mypy cannot see that through the TypeVar.
            doc = asdict(merge(held, row))  # type: ignore[call-overload]
            doc[id_field] = key
            doc["_id"] = key
            db[collection].replace_one({"_id": key}, doc, upsert=True)

    def put_service(self, row: Service) -> None:
        self._merge_put("services", row.service_id, row, merge_service, "service_id")

    def put_operation(self, row: Operation) -> None:
        self._merge_put("operations", row.op_id, row, merge_operation, "op_id")

    def put_sighting(self, row: Sighting) -> None:
        self._merge_put(
            "sightings", row.sighting_id, row, merge_sighting, "sighting_id"
        )

    def put_field(self, row: FieldObs) -> None:
        key = field_id(row.op_id, row.path)
        self._merge_put("fields", key, row, merge_field, "field_id")

    def put_offer(self, row: OfferObs) -> None:
        self._merge_put("offers", row.offer_id, row, merge_offer, "offer_id")

    def put_equivalence(self, row: Equivalence) -> None:
        key = equivalence_id(row)
        self._merge_put("equivalences", key, row, merge_equivalence, "equivalence_id")

    def put_health(self, row: Health) -> None:
        db = self._writable()
        with self._guard():
            doc = {**asdict(row), "_id": row.op_id}
            db["health"].replace_one({"_id": row.op_id}, doc, upsert=True)

    def append_event(self, row: Event) -> None:
        db = self._writable()
        with self._guard():
            db[EVENTS].insert_one(asdict(row))

    def _all(
        self, collection: str, cls: type[T], query: Mapping[str, Any] | None = None
    ) -> list[T]:
        with self._guard():
            docs = [dict(d) for d in self._db[collection].find(dict(query or {}))]
        return [_from_doc(cls, d) for d in docs]

    def services(self) -> list[Service]:
        return self._all("services", Service)

    def operations(self) -> list[Operation]:
        return self._all("operations", Operation)

    def sightings(self, catalog: Catalog | None = None) -> list[Sighting]:
        query = {} if catalog is None else {"catalog": catalog}
        return self._all("sightings", Sighting, query)

    def fields(self) -> list[FieldObs]:
        return self._all("fields", FieldObs)

    def offers(self) -> list[OfferObs]:
        return self._all("offers", OfferObs)

    def offers_for(self, op_id: str) -> list[OfferObs]:
        return self._all("offers", OfferObs, {"op_id": op_id})

    def health_for(self, op_id: str) -> Health | None:
        found = self._all("health", Health, {"_id": op_id})
        return found[0] if found else None

    def equivalences(self) -> list[Equivalence]:
        return self._all("equivalences", Equivalence)

    def health(self) -> list[Health]:
        return self._all("health", Health)

    def events(self) -> list[Event]:
        return self._all(EVENTS, Event)
