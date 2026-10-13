"""Every dataclass and Literal of Gecko's service registry. The only definition.

The registry holds PUBLIC METADATA about paid services: what they are, where they were
listed, what each operation claims to return and what real replies showed it returns.
It never holds a reply's values, a subject, a customer id or a secret (founder ruling,
9 Oct: field NAMES and TYPES seen in customer runs may feed it; nothing else may).

Evidence uses the provenance ladder of :mod:`gecko.provenance`, plus ``VERIFIED`` from
its verdict ladder: a catalog listing is ``CLAIMED``, a field seen in a real reply is
``EXTRACTED``, a paid canary that read it back is ``VERIFIED``. ``DECLARED`` is a human
(or a committed source map) saying so; ``INFERRED`` is a proposal nothing may route on.

Times are ISO-8601 UTC strings, as in :mod:`gecko.anyquery.types`, so lexical order is
time order. Money is integers: cents for catalog prices, base units for x402 amounts.

``Equivalence`` and ``EvidenceLevel`` moved here from the supplier source map, which
re-imports them; ``Catalog`` moved here from :mod:`gecko.anyquery.types`, which
re-exports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from gecko.provenance import Provenance
from gecko.x402_svm import Offer

__all__ = [
    "CATALOGS",
    "Catalog",
    "Claim",
    "EVENT_KINDS",
    "EVIDENCE_RANK",
    "Equivalence",
    "Event",
    "EventKind",
    "Evidence",
    "EvidenceLevel",
    "FieldObs",
    "Health",
    "HealthStatus",
    "JsonType",
    "OfferObs",
    "Operation",
    "ParamClaim",
    "ParamIn",
    "Service",
    "Sighting",
]

#: Where a listing was read. ``public`` is a free public API (GLEIF); ``openapi`` is a
#: spec Gecko ingested itself.
Catalog = Literal[
    "orthogonal",
    "paysh",
    "public",
    "bazaar",
    "x402scan",
    "masterkey",
    "openapi",
]
CATALOGS: tuple[Catalog, ...] = get_args(Catalog)

Evidence = Literal[Provenance, "VERIFIED"]
#: Higher is stronger. A merge keeps the strongest; nothing a catalog says can lift a
#: fact above ``CLAIMED``, and a later catalog re-import cannot lower an observation.
EVIDENCE_RANK: dict[Evidence, int] = {
    "CLAIMED": 0,
    "INFERRED": 1,
    "DECLARED": 2,
    "EXTRACTED": 3,
    "VERIFIED": 4,
}

#: The supplier source map's own two-step scale, kept as it is because its tests and
#: repair path read it. ``confirmed`` = read live; ``declared`` = listed only.
EvidenceLevel = Literal["confirmed", "declared"]

JsonType = Literal["string", "number", "integer", "boolean", "object", "array", "null"]
ParamIn = Literal["query", "body", "path"]
HealthStatus = Literal["unknown", "up", "down", "drifting", "quarantined"]
EventKind = Literal[
    "listed",
    "delisted",
    "price",
    "pay_to",
    "networks",
    "status",
    "field_missing",
    "field_added",
    "type_changed",
]
EVENT_KINDS: tuple[EventKind, ...] = get_args(EventKind)


@dataclass(frozen=True)
class Service:
    """A provider, by the slug a catalog lists it under (``crustdata``,
    ``paysponge/coingecko``). Joining one provider across catalogs is later work."""

    service_id: str
    name: str
    homepage: str | None
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class Operation:
    """One callable route. ``op_id`` is :func:`gecko.servicemap.ids.op_id`: sha256 of
    the method and the normalized URL template, so two catalogs listing the same route
    land on one operation with two sightings."""

    op_id: str
    service_id: str
    method: str  # upper case; GET/POST are the only ones the planner may use
    url_template: str
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class ParamClaim:
    name: str
    location: ParamIn
    required: bool
    #: The catalog's declared type ("array", "string") and its one-line description.
    #: Untrusted text; the description is how a planner learns an input's allowed values
    #: (``include``: "work_email, personal_email, phone").
    kind: str = ""
    note: str = ""


@dataclass(frozen=True)
class Claim:
    """What one catalog says about one operation. Untrusted, never final: text is
    capped here and sanitized again where it reaches a model."""

    title: str
    description: str
    use_case: str
    price_cents: int | None  # the most a call costs, rounded up; None = not listed
    min_price_cents: int | None
    dynamic_price: bool
    params: tuple[ParamClaim, ...]
    service_url: str | None


@dataclass(frozen=True)
class Sighting:
    """One catalog listing one operation, under that catalog's own name and path."""

    sighting_id: str
    op_id: str
    service_id: str
    catalog: Catalog
    api: str  # the catalog's name for the service
    method: str
    path: str  # the catalog's spelling, placeholders as written
    found_url: str  # where the listing was read: a catalog URL or a local file
    license: str | None
    claimed: Claim
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class FieldObs:
    """One response path of one operation. Never a value."""

    op_id: str
    path: str  # the ``supplier.readers.field_names`` grammar, folded
    json_type: JsonType | None
    evidence: Evidence
    claimed_by: tuple[Catalog, ...]  # catalogs that listed it; sorted
    first_seen: str
    last_seen: str
    last_missing: str | None  # a real reply that should have carried it and did not


@dataclass(frozen=True)
class OfferObs:
    """One x402 ``accepts[]`` offer as a live challenge showed it: the persistable
    part of :class:`gecko.x402_svm.Offer`, built by :meth:`from_offer`. The raw dict and
    the per-challenge memo are not kept: the memo changes on every challenge and the
    raw dict is the server's own text, neither of which is metadata about the route."""

    offer_id: str
    op_id: str
    scheme: str
    network: str
    asset: str
    amount: int  # base units
    pay_to: str
    max_timeout_seconds: int
    fee_payer: str | None
    evidence: Evidence
    first_seen: str
    last_seen: str

    @classmethod
    def from_offer(
        cls, offer: Offer, *, op_id: str, evidence: Evidence, at: str
    ) -> OfferObs:
        # Local import: ids imports this module for Equivalence.
        from .ids import offer_id

        return cls(
            offer_id=offer_id(op_id, offer.network, offer.asset, offer.pay_to),
            op_id=op_id,
            scheme=offer.scheme,
            network=offer.network,
            asset=offer.asset,
            amount=offer.amount,
            pay_to=offer.pay_to,
            max_timeout_seconds=offer.max_timeout_seconds,
            fee_payer=offer.fee_payer,
            evidence=evidence,
            first_seen=at,
            last_seen=at,
        )


@dataclass(frozen=True)
class Equivalence:
    """Field paths that mean the same thing, across sources. Each member is
    ``(source, path)``: a supplier source name today, an ``op_id`` in the registry.
    ``agreements`` counts live reads where the members gave the same answer."""

    meaning: str
    paths: tuple[tuple[str, str], ...]
    level: EvidenceLevel
    evidence: str
    agreements: int = 0


@dataclass(frozen=True)
class Health:
    op_id: str
    status: HealthStatus
    tier: Literal[0, 1, 2]
    checked_at: str
    last_ok: str | None
    last_http_status: int | None
    consecutive_failures: int
    quarantined_until: str | None = None  # set when pay_to changed; excluded until then


@dataclass(frozen=True)
class Event:
    """Append-only. ``before`` and ``after`` hold metadata only (a price, a pay_to, a
    path), never a reply value."""

    at: str
    kind: EventKind
    op_id: str | None
    service_id: str
    catalog: Catalog | None
    before: str | None
    after: str | None
    note: str = ""
