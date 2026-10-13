"""What a landed transaction moved, read from ``getTransaction`` (json encoding) alone.

The independent check behind every ``landed`` ledger entry: a charge or a settlement
counts only once the chain, read back over RPC, shows the money where it was meant to go.
Pure functions over the node's answer; no network here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["account_keys", "lamport_delta", "token_delta"]


def token_delta(meta: Mapping[str, Any], *, owner: str, mint: str) -> int:
    """``post - pre`` raw units of ``mint`` held by token accounts ``owner`` owns."""

    def total(rows: Any) -> int:
        out = 0
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            if row.get("owner") != owner or row.get("mint") != mint:
                continue
            amount = (row.get("uiTokenAmount") or {}).get("amount")
            out += int(amount) if isinstance(amount, str) and amount.isdigit() else 0
        return out

    return total(meta.get("postTokenBalances")) - total(meta.get("preTokenBalances"))


def account_keys(result: Mapping[str, Any]) -> list[str]:
    """Static keys, then lookup-table keys (writable, readonly): balance-array order."""
    message = (result.get("transaction") or {}).get("message") or {}
    keys = [str(k) for k in message.get("accountKeys") or ()]
    loaded = (result.get("meta") or {}).get("loadedAddresses") or {}
    keys += [str(k) for k in loaded.get("writable") or ()]
    keys += [str(k) for k in loaded.get("readonly") or ()]
    return keys


def lamport_delta(result: Mapping[str, Any], address: str) -> int:
    """Lamports ``address`` gained (negative: lost) in this transaction."""
    meta = result.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    gained = 0
    for index, key in enumerate(account_keys(result)):
        if key == address and index < min(len(pre), len(post)):
            gained += int(post[index]) - int(pre[index])
    return gained
