"""Stage 1 — project a comprehended program Surface into the Mongo catalog.

A ``ProgramSurface`` is source-agnostic: :func:`let_me_buy_surface` builds v1's
from the known instructions, and an Orquestra-IDL builder will later produce the
same shape for any catalogued program WITHOUT touching :func:`sync_surface`.
That is the abstraction test (architecture §1): adding program #2 must add an
adapter + data, never edit the sync.

What lands in Mongo is SURFACE + provenance only — instruction names, argument
TYPES (never values), which args ground which accounts. No payloads, no secrets.
The three collections mirror architecture §2:

* ``catalog_programs``  — one doc per program surface.
* ``catalog_endpoints`` — one doc per instruction (the scoring unit).
* ``agent_specs``       — the immutable, ``spec_rev``-pinned tool-def manifest.

The sync is idempotent: re-running with the same ``spec_rev`` replaces the
program/endpoint docs (a store count may have changed) and is a no-op on the
immutable ``agent_specs`` doc (same content hash → same _id).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .collections import Collection


@dataclass(frozen=True)
class Endpoint:
    """One instruction as a scoring unit: its argument shape and what it grounds.

    ``arg_shape`` maps the caller-supplied argument NAMES to JSON types — never
    values. ``account_inputs`` are the accounts the instruction needs (derived,
    not passed as data); ``groundable_route_args`` are the args that DERIVE an
    account (e.g. ``store_name`` grounds the receipts PDA), the join Gecko
    recovers and the raw IDL drops.
    """

    operation_id: str
    arg_shape: dict[str, str]
    required: tuple[str, ...]
    account_inputs: tuple[str, ...] = field(default=())
    groundable_route_args: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ProgramSurface:
    """A comprehended program, ready to project. Source-agnostic by construction."""

    surface_id: str
    program_id: str
    network: str
    provider_id: str
    display: str
    endpoints: tuple[Endpoint, ...]


# ---------------------------------------------------------------- v1 builder
_LET_ME_BUY_PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"


def let_me_buy_surface(
    *,
    provider_id: str = "gecko",
    network: str = "mainnet",
) -> ProgramSurface:
    """v1's Surface for let_me_buy — the four instructions we have proven.

    Argument types are from the program's own IDL as exercised in
    ``scripts/roundtrip_let_me_buy.py`` (store_name/product_name = string,
    price = u64, table_number = u8, receipt_id = u64, pubkeys for authority/
    mint). ``make_purchase`` is the proven-on-mainnet one and carries the
    store_name→receipts-PDA join in ``groundable_route_args``.
    """
    endpoints = (
        Endpoint(
            operation_id="initialize",
            arg_shape={"store_name": "string", "authority": "pubkey"},
            required=("store_name", "authority"),
            account_inputs=("receipts", "authority", "system_program"),
            groundable_route_args=("store_name",),
        ),
        Endpoint(
            operation_id="add_product",
            arg_shape={
                "store_name": "string",
                "authority": "pubkey",
                "name": "string",
                "price": "u64",
                "mint": "pubkey",
            },
            required=("store_name", "authority", "name", "price", "mint"),
            account_inputs=("receipts", "authority", "mint", "system_program"),
            groundable_route_args=("store_name",),
        ),
        Endpoint(
            operation_id="make_purchase",
            arg_shape={
                "store_name": "string",
                "product_name": "string",
                "table_number": "u8",
            },
            required=("store_name", "product_name", "table_number"),
            account_inputs=(
                "receipts",
                "signer",
                "authority",
                "mint",
                "sender_token_account",
                "recipient_token_account",
                "token_program",
                "system_program",
                "associated_token_program",
            ),
            groundable_route_args=("store_name",),
        ),
        Endpoint(
            operation_id="mark_as_delivered",
            arg_shape={
                "store_name": "string",
                "receipt_id": "u64",
                "authority": "pubkey",
            },
            required=("store_name", "receipt_id", "authority"),
            account_inputs=("receipts", "authority"),
            groundable_route_args=("store_name",),
        ),
    )
    return ProgramSurface(
        surface_id="orquestra:let_me_buy",
        program_id=_LET_ME_BUY_PROGRAM_ID,
        network=network,
        provider_id=provider_id,
        display="let_me_buy",
        endpoints=endpoints,
    )


# ---------------------------------------------------------------- spec_rev
def surface_spec_rev(surface: ProgramSurface) -> str:
    """A content hash over the surface's tool-def shape — the immutable spec id.

    Deterministic and value-free: it hashes operation ids + arg shapes + required
    + grounding, sorted, so the same surface always yields the same ``spec_rev``
    and a scored number is always reproducible against the exact defs. It does NOT
    include the network or the live store count — those change without changing
    the tool contract, and a spec_rev must pin the CONTRACT, not the world.
    """
    shape = [
        {
            "op": e.operation_id,
            "arg_shape": dict(sorted(e.arg_shape.items())),
            "required": sorted(e.required),
            "grounds": sorted(e.groundable_route_args),
        }
        for e in sorted(surface.endpoints, key=lambda e: e.operation_id)
    ]
    payload = json.dumps(
        {"program_id": surface.program_id, "endpoints": shape},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sr_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- sync
def sync_surface(
    surface: ProgramSurface,
    *,
    programs: Collection,
    endpoints: Collection,
    agent_specs: Collection,
) -> str:
    """Project ``surface`` into the three catalog collections. Returns the spec_rev.

    Idempotent by construction: the program and endpoint docs are keyed by
    ``surface_id`` / ``surface_id:operation_id`` (a re-sync overwrites — v1's
    in-memory collection appends, which the Mongo adapter will make an upsert),
    and the ``agent_specs`` doc is keyed by ``surface_id:spec_rev`` (immutable —
    re-syncing the same surface writes the same _id, so it is a no-op).
    """
    spec_rev = surface_spec_rev(surface)

    programs.insert_one(
        {
            "_id": surface.surface_id,
            "surface_id": surface.surface_id,
            "program_id": surface.program_id,
            "network": surface.network,
            "provider_id": surface.provider_id,
            "display": surface.display,
            "latest_spec_rev": spec_rev,
            "endpoint_count": len(surface.endpoints),
        }
    )

    for endpoint in surface.endpoints:
        endpoints.insert_one(
            {
                "_id": f"{surface.surface_id}:{endpoint.operation_id}",
                "surface_id": surface.surface_id,
                "operation_id": endpoint.operation_id,
                "arg_shape": dict(endpoint.arg_shape),
                "required": list(endpoint.required),
                "account_inputs": list(endpoint.account_inputs),
                "groundable_route_args": list(endpoint.groundable_route_args),
                "spec_rev": spec_rev,
            }
        )

    if agent_specs.count_documents({"_id": f"{surface.surface_id}:{spec_rev}"}) == 0:
        agent_specs.insert_one(
            {
                "_id": f"{surface.surface_id}:{spec_rev}",
                "surface_id": surface.surface_id,
                "spec_rev": spec_rev,
                "tool_def_names": [e.operation_id for e in surface.endpoints],
            }
        )
    return spec_rev
