"""The Sprint-2 resolution engine — fill resolver seeds by control-plane reads.

A :class:`~gecko.pda.ResolverPdaSeedNode` is Gecko's honest placeholder for a seed
it could not resolve statically — most importantly a *dotted-path* seed like
``bonding_curve.creator``: a field that lives inside ANOTHER account's runtime
data, which no IDL/llms.txt carries (Anchor #4057). Deriving such a PDA needs one
extra step the spec cannot give: read the source account and pull the field out.

This module does exactly that, and NOTHING more. Given a resolver seed that carries
a ``resolve`` recipe, it:

  1. derives the ADDRESS of the source account (itself a PDA in the same graph,
     e.g. ``bonding_curve`` from ``mint``);
  2. reads that account's PUBLIC metadata via ``getAccountInfo`` — the field pubkey
     at a byte offset, or the account owner;
  3. binds the read value and derives the target PDA with a now-complete account set.

Control-plane invariant #1 holds exactly as in :mod:`gecko.pda_testkit`: reads are
``getAccountInfo`` of PUBLIC on-chain metadata, decoded IN MEMORY and NEVER
persisted. The RPC is injectable (``rpc_call``) so the whole engine is unit-testable
offline with no network; the default posts JSON-RPC to a LOCAL surfpool fork
(loopback — exempt from the anti-SSRF spec rules, which guard ingestion of untrusted
specs, not our own validator; see :mod:`gecko.pda_testkit`).

Scope: this resolves RESOLVER seeds via reads. Instruction-level account wiring —
which account is the ATA's ``owner``, etc. — is the Sprint-3 plan, not here.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from typing import Any, Mapping

from .pda import (
    DerivedPda,
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    derive_pda,
)
from .pda_testkit import LOCAL_RPC, RpcCall, _default_rpc_call

__all__ = [
    "ResolveError",
    "read_account_owner",
    "read_account_field_pubkey",
    "resolve_pda",
]


class ResolveError(Exception):
    """Raised when a resolver read fails — a missing source account, a truncated
    field, or a recipe that cannot be honoured.

    Messages carry only public data (account addresses, byte offsets) — never a
    secret; on-chain metadata reads have no secret material (invariant #1)."""


def _account_value(
    address: str,
    encoding: str | None,
    *,
    rpc_url: str,
    rpc_call: RpcCall | None,
) -> dict[str, Any]:
    """``getAccountInfo`` → the ``value`` object, or raise if the account is absent."""
    call = rpc_call or _default_rpc_call
    params: list[Any] = [address]
    if encoding is not None:
        params.append({"encoding": encoding})
    resp = call(rpc_url, "getAccountInfo", params)
    value = (resp.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise ResolveError(
            f"account {address} not found on-chain — cannot resolve seed from it"
        )
    return value


def read_account_owner(
    address: str,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> str:
    """The base58 program that OWNS ``address`` — public metadata, read in memory.

    Used to resolve seeds like a mint's ``token_program`` (a Token vs Token-2022
    mint is distinguished by its owner). Raises :class:`ResolveError` if the account
    does not exist.
    """
    value = _account_value(address, None, rpc_url=rpc_url, rpc_call=rpc_call)
    owner = value.get("owner")
    if not isinstance(owner, str):
        raise ResolveError(f"account {address} has no owner in getAccountInfo result")
    return owner


def read_account_field_pubkey(
    address: str,
    offset: int,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> str:
    """The 32-byte pubkey stored at byte ``offset`` inside ``address``'s data, as base58.

    This is the dotted-path read: e.g. ``bonding_curve.creator`` lives at offset 49
    (8-byte Anchor discriminator + 5×u64 + 1 ``complete`` bool). Only the 32 bytes at
    the offset are decoded — the account payload is never stored (invariant #1).
    Raises :class:`ResolveError` if the account is missing or its data is too short.
    """
    value = _account_value(address, "base64", rpc_url=rpc_url, rpc_call=rpc_call)
    data = value.get("data")
    if not (isinstance(data, list) and data and isinstance(data[0], str)):
        raise ResolveError(
            f"account {address} has no base64 data to read a field pubkey from"
        )
    raw = base64.b64decode(data[0])
    field = raw[offset : offset + 32]
    if len(field) < 32:
        raise ResolveError(
            f"account {address} data is {len(raw)} bytes — too short to read a "
            f"32-byte pubkey at offset {offset}"
        )
    # Lazy solders import (behind the [solana] extra) mirrors gecko.pda.
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover - needs the [solana] extra
        raise ResolveError(
            "reading a field pubkey needs the 'solana' extra: install with "
            "`pip install gecko-surf[solana]` (or `uv add solders`)"
        ) from exc
    return str(Pubkey.from_bytes(field))


def resolve_pda(
    pdas: dict[str, PdaNode],
    account: str,
    bindings: Mapping[str, Any],
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> DerivedPda:
    """Derive ``pdas[account]``, filling each resolver seed that carries a ``resolve``
    recipe via a control-plane on-chain read, then derive with a complete account set.

    For each :class:`~gecko.pda.ResolverPdaSeedNode` with a ``resolve`` recipe:

    - the source account's ADDRESS is derived (``pdas[recipe["read"]]`` — itself a
      PDA in the graph, derivable from ``bindings``, e.g. ``bonding_curve`` from
      ``mint``);
    - ``field_offset`` → read the pubkey at that byte offset; ``owner`` → read the
      account owner;
    - the read value is bound under the seed's name.

    The node is then rebuilt with each resolved resolver seed replaced by a plain
    pubkey :class:`~gecko.pda.VariablePdaSeedNode`, and derived. Constants,
    variables, and ordered pairs pass through unchanged.
    """
    node = pdas[account]
    working: dict[str, Any] = dict(bindings)
    resolved_names: set[str] = set()

    for seed in node.seeds:
        if not isinstance(seed, ResolverPdaSeedNode):
            continue
        recipe = seed.resolve
        if recipe is None:
            # An unresolved seed with no recipe — leave it for derive_pda to reject
            # honestly (UnresolvedSeedError). We never fabricate a value.
            continue
        source_key = recipe.get("read")
        if source_key not in pdas:
            raise ResolveError(
                f"resolver seed {seed.name!r} reads {source_key!r}, "
                f"which is not a PDA in this program's graph"
            )
        # The source is itself a PDA in this graph — pure derivation, no read.
        source_addr = derive_pda(pdas[source_key], working).address

        if "field_offset" in recipe:
            value = read_account_field_pubkey(
                source_addr,
                int(recipe["field_offset"]),
                rpc_url=rpc_url,
                rpc_call=rpc_call,
            )
        elif recipe.get("owner"):
            value = read_account_owner(source_addr, rpc_url=rpc_url, rpc_call=rpc_call)
        else:
            raise ResolveError(
                f"resolver seed {seed.name!r} has a recipe with neither "
                f"'field_offset' nor 'owner' — nothing to read"
            )
        working[seed.name] = value
        resolved_names.add(seed.name)

    derivable_seeds = tuple(
        VariablePdaSeedNode(s.name, source="account", encoding="pubkey")
        if isinstance(s, ResolverPdaSeedNode) and s.name in resolved_names
        else s
        for s in node.seeds
    )
    derivable = replace(node, seeds=derivable_seeds)
    return derive_pda(derivable, working)
