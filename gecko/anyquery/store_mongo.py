"""The Mongo layout for any-query, as data: one database per customer.

The database is ``gq_<sha256(account_id)[:24]>``. It is created by the first write and
removed with ``dropDatabase`` on a deletion request, so "deletable" is one call and no
query can reach two customers. ``MongoStore`` itself is lane 2; this module fixes the
names and indexes it must use.

What Mongo may hold: ``calls`` holds field names and counts, never values; ``payments``
holds no secrets; no Privy auth material or Helius key is written anywhere. Atlas shared
tiers cap databases and collections, so at nine collections per customer (``datasets``
and ``dataset_rows`` made it nine) the tier sets the customer ceiling: check it before
the first deploy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "COLLECTIONS",
    "CollectionSpec",
    "IndexSpec",
    "db_name_for",
    "ensure_indexes",
]

Direction = Literal[1, -1]


@dataclass(frozen=True)
class IndexSpec:
    name: str
    keys: tuple[tuple[str, Direction], ...]
    unique: bool = False
    partial: dict[str, Any] | None = None
    #: A TTL index: Mongo deletes a document this long after the date in its one key.
    #: The key must hold a BSON date, not an ISO string, or nothing ever expires.
    expire_after_s: int | None = None


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    id_field: (
        str | None
    )  # the dataclass field stored as _id; None means Mongo assigns it
    indexes: tuple[IndexSpec, ...]


COLLECTIONS: tuple[CollectionSpec, ...] = (
    CollectionSpec(
        "plans", "plan_id", (IndexSpec("created_at", (("created_at", -1),)),)
    ),
    CollectionSpec("quotes", "quote_id", (IndexSpec("plan", (("plan_id", 1),)),)),
    CollectionSpec("bundles", "name", ()),
    # A customer's living tables: _id is the slug, unique per customer because the
    # database is the customer's. One row document per (dataset, subject).
    CollectionSpec("datasets", "dataset_id", ()),
    CollectionSpec(
        "dataset_rows",
        "row_id",
        (
            IndexSpec(
                "one_row_per_subject",
                (("dataset_id", 1), ("subject", 1)),
                unique=True,
            ),
        ),
    ),
    CollectionSpec(
        "runs",
        "run_id",
        (
            IndexSpec("one_run_per_quote", (("quote_id", 1),), unique=True),
            IndexSpec("plan_runs", (("plan_id", 1), ("run_id", -1))),
        ),
    ),
    CollectionSpec(
        "facts",
        None,
        (
            IndexSpec(
                "one_fact_per_cell",
                (("run_id", 1), ("subject", 1), ("field", 1)),
                unique=True,
            ),
            IndexSpec(
                "cell_history",
                (("plan_id", 1), ("subject", 1), ("field", 1), ("run_id", -1)),
            ),
        ),
    ),
    CollectionSpec(
        "calls",
        None,
        (
            IndexSpec("run", (("run_id", 1),)),
            IndexSpec("drift_baseline", (("api", 1), ("path", 1), ("ts", -1))),
        ),
    ),
    CollectionSpec(
        "payments",
        "entry_id",
        (
            # $in inside a partial filter needs MongoDB 6.0+ (Atlas default is newer).
            IndexSpec(
                "one_charge_per_run",
                (("run_id", 1), ("kind", 1)),
                unique=True,
                partial={"kind": {"$in": ["charge", "discount"]}},
            ),
            # A separate index, not a wider partial filter on the one above: changing
            # an existing index's options makes createIndex fail on a live database.
            IndexSpec(
                "one_credit_per_run",
                (("run_id", 1), ("kind", 1)),
                unique=True,
                partial={"kind": "run_credit"},
            ),
            # Settlement attempts are not unique per run (a failed one is retried), but
            # only one may be OPEN: the guard against two settlers signing for one charge.
            IndexSpec(
                "one_open_settlement_per_run",
                (("run_id", 1), ("kind", 1)),
                unique=True,
                partial={"kind": "settlement", "status": "pending"},
            ),
            # Partial on $type string, NOT sparse: LedgerEntry.signature is None until
            # send, and a sparse index still indexes an explicit null, so the second
            # pending entry would collide with the first.
            IndexSpec(
                "one_entry_per_signature",
                (("signature", 1),),
                unique=True,
                partial={"signature": {"$type": "string"}},
            ),
            IndexSpec("recent_by_kind", (("kind", 1), ("created_at", -1))),
        ),
    ),
)


def db_name_for(customer: str, prefix: str = "gq_") -> str:
    """``prefix + sha256(customer)[:24]``: the account id never appears in a db name."""
    if not isinstance(customer, str) or not customer.strip():
        raise ValueError("customer must be a non-empty account id")
    return prefix + hashlib.sha256(customer.encode("utf-8")).hexdigest()[:24]


def ensure_indexes(
    db: Any, collections: tuple[CollectionSpec, ...] = COLLECTIONS
) -> None:
    """Create every index in ``collections`` on ``db``. Idempotent: Mongo treats an
    identical ``create_index`` as a no-op, so this runs on the first write per customer."""
    for spec in collections:
        collection = db[spec.name]
        for index in spec.indexes:
            options: dict[str, Any] = {"name": index.name, "unique": index.unique}
            if index.partial is not None:
                options["partialFilterExpression"] = index.partial
            if index.expire_after_s is not None:
                options["expireAfterSeconds"] = index.expire_after_s
            collection.create_index(list(index.keys), **options)
