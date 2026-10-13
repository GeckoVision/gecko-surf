"""The Mongo layout of the service registry, as data: one shared database.

``gecko_registry`` holds public metadata about services, never a customer's anything,
which is why it is one database and not one per customer like any-query's ``gq_*``. It
stays private (founder ruling, 9 Oct). Field names and types from customer runs may be
written here; values, subjects and customer ids may not, and no collection has a key
for them.

``events`` is append-only: the store only ever inserts into it. ``probes`` keeps 30
days of challenge-probe headers through a TTL index, whose key must be a BSON date.
``control`` holds one document, the kill switch, read before any write.
"""

from __future__ import annotations

from typing import Any

from gecko.anyquery.store_mongo import CollectionSpec, IndexSpec
from gecko.anyquery.store_mongo import ensure_indexes as _ensure

__all__ = [
    "COLLECTIONS",
    "CONTROL",
    "DB_NAME",
    "EVENTS",
    "KILL_SWITCH_ID",
    "PROBES",
    "PROBE_TTL_S",
    "ensure_indexes",
]

DB_NAME = "gecko_registry"
EVENTS = "events"
PROBES = "probes"
CONTROL = "control"
KILL_SWITCH_ID = "kill_switch"
PROBE_TTL_S = 30 * 24 * 3600

COLLECTIONS: tuple[CollectionSpec, ...] = (
    CollectionSpec(
        "services", "service_id", (IndexSpec("last_seen", (("last_seen", -1),)),)
    ),
    CollectionSpec(
        "operations", "op_id", (IndexSpec("service", (("service_id", 1),)),)
    ),
    CollectionSpec(
        "sightings",
        "sighting_id",
        (
            # The ledger importer resolves an observed (catalog, api, path) through this.
            IndexSpec("listing", (("catalog", 1), ("api", 1), ("path", 1))),
            IndexSpec("op", (("op_id", 1),)),
        ),
    ),
    CollectionSpec(
        "fields",
        "field_id",
        (
            IndexSpec("one_per_op_path", (("op_id", 1), ("path", 1)), unique=True),
            IndexSpec("path", (("path", 1),)),
        ),
    ),
    CollectionSpec(
        "offers",
        "offer_id",
        (IndexSpec("op", (("op_id", 1),)), IndexSpec("pay_to", (("pay_to", 1),))),
    ),
    CollectionSpec(
        "equivalences",
        "equivalence_id",
        (IndexSpec("meaning", (("meaning", 1),)),),
    ),
    CollectionSpec("health", "op_id", (IndexSpec("status", (("status", 1),)),)),
    CollectionSpec(
        EVENTS,
        None,
        (
            IndexSpec("op_history", (("op_id", 1), ("at", -1))),
            IndexSpec("by_kind", (("kind", 1), ("at", -1))),
        ),
    ),
    CollectionSpec(
        PROBES,
        None,
        (
            IndexSpec("expire", (("probed_at", 1),), expire_after_s=PROBE_TTL_S),
            IndexSpec("op", (("op_id", 1), ("probed_at", -1))),
        ),
    ),
    CollectionSpec(CONTROL, None, ()),
)


def ensure_indexes(db: Any) -> None:
    """Every registry index on ``db``. Idempotent."""
    _ensure(db, COLLECTIONS)
