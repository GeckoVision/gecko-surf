"""The registry contract, and :class:`MemoryRegistry`, its in-memory implementation.

Every ``put_*`` merges with the row already held (:mod:`gecko.servicemap.merge`); events
are only ever appended. The kill switch is a control row: when it is engaged, a writer
refuses before it writes anything (:class:`RegistryKilledError`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from .ids import equivalence_id, field_id
from .merge import (
    merge_equivalence,
    merge_field,
    merge_offer,
    merge_operation,
    merge_service,
    merge_sighting,
)
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

__all__ = [
    "MemoryRegistry",
    "Registry",
    "RegistryError",
    "RegistryKilledError",
    "RegistryUnavailableError",
]

T = TypeVar("T")


class RegistryError(Exception):
    """A registry read or write was refused."""


class RegistryKilledError(RegistryError):
    """The control row's kill switch is engaged; nothing was written."""


class RegistryUnavailableError(RegistryError):
    """The store did not answer. Never carries a connection string."""


class Registry(Protocol):
    def killed(self) -> bool: ...

    def put_service(self, row: Service) -> None: ...

    def put_operation(self, row: Operation) -> None: ...

    def put_sighting(self, row: Sighting) -> None: ...

    def put_field(self, row: FieldObs) -> None: ...

    def put_offer(self, row: OfferObs) -> None: ...

    def put_equivalence(self, row: Equivalence) -> None: ...

    def put_health(self, row: Health) -> None: ...

    def append_event(self, row: Event) -> None: ...

    def services(self) -> list[Service]: ...

    def operations(self) -> list[Operation]: ...

    def sightings(self, catalog: Catalog | None = None) -> list[Sighting]: ...

    def fields(self) -> list[FieldObs]: ...

    def offers(self) -> list[OfferObs]: ...

    def offers_for(self, op_id: str) -> list[OfferObs]: ...

    def health_for(self, op_id: str) -> Health | None: ...

    def equivalences(self) -> list[Equivalence]: ...

    def health(self) -> list[Health]: ...

    def events(self) -> list[Event]: ...


def _merge_into(
    table: dict[str, T], key: str, row: T, merge: Callable[[T | None, T], T]
) -> None:
    table[key] = merge(table.get(key), row)


@dataclass
class MemoryRegistry:
    """The registry in dicts. The test fake, and the dry-run target of the importer."""

    kill_switch: bool = False
    _services: dict[str, Service] = field(default_factory=dict)
    _operations: dict[str, Operation] = field(default_factory=dict)
    _sightings: dict[str, Sighting] = field(default_factory=dict)
    _fields: dict[str, FieldObs] = field(default_factory=dict)
    _offers: dict[str, OfferObs] = field(default_factory=dict)
    _equivalences: dict[str, Equivalence] = field(default_factory=dict)
    _health: dict[str, Health] = field(default_factory=dict)
    _events: list[Event] = field(default_factory=list)

    def _writable(self) -> None:
        if self.kill_switch:
            raise RegistryKilledError("the registry kill switch is engaged")

    def killed(self) -> bool:
        return self.kill_switch

    def put_service(self, row: Service) -> None:
        self._writable()
        _merge_into(self._services, row.service_id, row, merge_service)

    def put_operation(self, row: Operation) -> None:
        self._writable()
        _merge_into(self._operations, row.op_id, row, merge_operation)

    def put_sighting(self, row: Sighting) -> None:
        self._writable()
        _merge_into(self._sightings, row.sighting_id, row, merge_sighting)

    def put_field(self, row: FieldObs) -> None:
        self._writable()
        _merge_into(self._fields, field_id(row.op_id, row.path), row, merge_field)

    def put_offer(self, row: OfferObs) -> None:
        self._writable()
        _merge_into(self._offers, row.offer_id, row, merge_offer)

    def put_equivalence(self, row: Equivalence) -> None:
        self._writable()
        _merge_into(self._equivalences, equivalence_id(row), row, merge_equivalence)

    def put_health(self, row: Health) -> None:
        self._writable()
        self._health[row.op_id] = row

    def append_event(self, row: Event) -> None:
        self._writable()
        self._events.append(row)

    def services(self) -> list[Service]:
        return list(self._services.values())

    def operations(self) -> list[Operation]:
        return list(self._operations.values())

    def sightings(self, catalog: Catalog | None = None) -> list[Sighting]:
        return [
            s
            for s in self._sightings.values()
            if catalog is None or s.catalog == catalog
        ]

    def fields(self) -> list[FieldObs]:
        return list(self._fields.values())

    def offers(self) -> list[OfferObs]:
        return list(self._offers.values())

    def offers_for(self, op_id: str) -> list[OfferObs]:
        return [o for o in self._offers.values() if o.op_id == op_id]

    def health_for(self, op_id: str) -> Health | None:
        return self._health.get(op_id)

    def equivalences(self) -> list[Equivalence]:
        return list(self._equivalences.values())

    def health(self) -> list[Health]:
        return list(self._health.values())

    def events(self) -> list[Event]:
        return list(self._events)
