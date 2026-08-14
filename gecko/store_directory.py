"""Every let_me_buy storefront on a network, read from the chain — never from a wired list.

``prepare_purchase`` re-reads the single store it is asked for, and its refusal states the
reason exactly: the store's authority and mint live INSIDE the store's account, and a
keyless path that reads no chain state can only guess them. This module is the read that
refusal asks for: enumerate the program's accounts (``getProgramAccounts`` already filters
by owner, so every row IS owned by let_me_buy), decode each one against the layout the
program's own IDL declares, and hand back names, products and prices.

WHAT IS TRUSTED, STATED ONCE. The account CONTENTS come from one unauthenticated node —
the same trust position as ``prepare_purchase``'s simulation. That is acceptable for one
reason: this module's output is a MENU, not an authorization. Nothing downstream may treat
a listed authority as verified. A purchase does not act on this list at all: it names one
store, ``resolve_store`` re-reads THAT store's own account, and the whole loop (plan
refusal, simulate, binding, spend policy) runs before anything signs. So a poisoned or
stale menu can mislead a reader about what exists; it cannot decide who gets paid.

WHAT IS NOT TRUSTED. Every byte is treated as hostile transport output: the Borsh cursor
is bounds-checked, string lengths are capped, account and product counts are capped, and an
account that does not decode is SKIPPED AND COUNTED — never guessed at, never dropped
silently. A directory that had to skip rows says so on the tin.

Control plane only: store names, products and prices are the program's public surface —
the on-chain equivalent of an API's catalog page. Nothing here is persisted, and nothing
here reads a balance, a receipt entry, or any buyer's data (the receipts vec is skipped
field-by-field without being returned).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from .networks import UNKNOWN_NETWORK, coerce_network
from .prepare_purchase import USDC_MINT, _resolve_rpc_url
from .rpc import RpcCall, RpcError, default_rpc_call

__all__ = [
    "LET_ME_BUY_PROGRAM_ID",
    "LIST_STORES_TOOL",
    "StoreDecodeError",
    "StoreListing",
    "StoreProduct",
    "decode_store",
    "list_stores",
    "list_stores_result",
]

#: The deployed storefront program this directory enumerates.
LET_ME_BUY_PROGRAM_ID = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"

#: Anchor's 8-byte account discriminator, skipped before the Borsh payload.
_DISCRIMINATOR_BYTES = 8

#: Caps on untrusted counts. A node that reports more of anything than these is not
#: answering the question this module asks — the row (or the run) refuses rather than
#: looping on attacker-chosen lengths.
_MAX_STRING_BYTES = 4_096
_MAX_PRODUCTS = 256
_MAX_RECEIPT_ROWS = 100_000
_MAX_ACCOUNTS = 512


class StoreDecodeError(Exception):
    """One account's bytes are not this layout. The row is skipped, never guessed at."""


class _Cursor:
    """A bounds-checked Borsh reader. Every read past the end raises, nothing wraps."""

    def __init__(self, raw: bytes, position: int = 0) -> None:
        self._raw = raw
        self._position = position

    def take(self, count: int) -> bytes:
        end = self._position + count
        if count < 0 or end > len(self._raw):
            raise StoreDecodeError("the account ended mid-field; it is not this layout")
        chunk = self._raw[self._position : end]
        self._position = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "little")

    def string(self) -> str:
        length = self.u32()
        if length > _MAX_STRING_BYTES:
            raise StoreDecodeError("a string field declares an implausible length")
        return self.take(length).decode("utf-8", "replace")

    def pubkey_base58(self) -> str:
        return _b58encode(self.take(32))


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    """Base58 for a 32-byte key. Local so this module needs no solana dependency."""
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, digit = divmod(value, 58)
        encoded = _B58_ALPHABET[digit] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + (encoded or "")


@dataclass(frozen=True)
class StoreProduct:
    """One listed product: its name and its price in the mint's own units."""

    name: str
    price_raw: int
    decimals: int
    mint: str

    @property
    def price_ui(self) -> str:
        """The price rendered at the mint's declared scale — display only, never math."""
        scaled = self.price_raw / 10**self.decimals
        return f"{scaled:.{self.decimals}f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class StoreListing:
    """One storefront, decoded from its own account. A MENU entry, not an authorization."""

    store_name: str
    address: str
    authority: str
    total_purchases: int
    products: tuple[StoreProduct, ...]


def decode_store(raw: bytes, *, address: str) -> StoreListing:
    """Decode one ``Receipts`` account. Raises :class:`StoreDecodeError`, never guesses.

    The layout is the program IDL's own (``Receipts``): a receipts vec (skipped field by
    field — buyer data never enters the return value), ``total_purchases``, the store
    name, the authority, and the products vec.
    """
    cursor = _Cursor(raw, _DISCRIMINATOR_BYTES)
    receipt_rows = cursor.u32()
    if receipt_rows > _MAX_RECEIPT_ROWS:
        raise StoreDecodeError("the receipts vec declares an implausible length")
    for _ in range(receipt_rows):
        cursor.u64()  # receipt_id
        cursor.take(32)  # buyer — skipped, never returned
        cursor.u8()  # was_delivered
        cursor.u64()  # price
        cursor.u64()  # timestamp
        cursor.u8()  # table_number
        cursor.string()  # product_name
    total_purchases = cursor.u64()
    store_name = cursor.string()
    authority = cursor.pubkey_base58()
    product_count = cursor.u32()
    if product_count > _MAX_PRODUCTS:
        raise StoreDecodeError("the products vec declares an implausible length")
    products = []
    for _ in range(product_count):
        price_raw = cursor.u64()
        decimals = cursor.u8()
        mint = cursor.pubkey_base58()
        products.append(
            StoreProduct(
                name=cursor.string(), price_raw=price_raw, decimals=decimals, mint=mint
            )
        )
    return StoreListing(
        store_name=store_name,
        address=address,
        authority=authority,
        total_purchases=total_purchases,
        products=tuple(products),
    )


def list_stores(
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    product: str | None = None,
) -> dict[str, Any]:
    """Every decodable storefront at ``rpc_url``, optionally filtered by product name.

    Returns a plain dict shaped for the MCP surface: ``stores`` (each with its products
    and prices), ``skipped`` (accounts that did not decode — counted, never guessed at),
    and ``truncated`` (True when the node returned more accounts than the cap and the
    tail was NOT read; a partial directory says so rather than passing as complete).

    ``product`` filters case-insensitively on a substring, so "water" finds both
    "Water" and "Sparkling water". The filter is applied AFTER decoding: a store with no
    match is omitted, not an error.

    ``rpc_url`` is expected to be validated by the caller (the MCP surface routes it
    through the same SSRF guard ``prepare_purchase`` uses).
    """
    call = rpc_call or default_rpc_call
    response = call(
        rpc_url,
        "getProgramAccounts",
        [LET_ME_BUY_PROGRAM_ID, {"encoding": "base64"}],
    )
    rows = response.get("result")
    if not isinstance(rows, list):
        raise RpcError("getProgramAccounts returned no account list")

    truncated = len(rows) > _MAX_ACCOUNTS
    needle = (
        product.strip().lower()
        if isinstance(product, str) and product.strip()
        else None
    )

    stores: list[dict[str, Any]] = []
    skipped = 0
    for row in rows[:_MAX_ACCOUNTS]:
        try:
            account = row["account"]
            raw = base64.b64decode(account["data"][0])
            listing = decode_store(raw, address=str(row["pubkey"]))
        except (StoreDecodeError, KeyError, TypeError, ValueError, IndexError):
            # Not this layout, or not the shape a node answers with. Counted, not guessed.
            skipped += 1
            continue
        products = [
            {
                "name": entry.name,
                "price_raw": entry.price_raw,
                "decimals": entry.decimals,
                "price_ui": entry.price_ui,
                "mint": entry.mint,
                "mint_note": "USDC" if entry.mint == USDC_MINT else None,
            }
            for entry in listing.products
            if needle is None or needle in entry.name.lower()
        ]
        if needle is not None and not products:
            continue
        stores.append(
            {
                "store": listing.store_name,
                "address": listing.address,
                "authority": listing.authority,
                "total_purchases": listing.total_purchases,
                "products": products,
            }
        )

    return {
        "program": LET_ME_BUY_PROGRAM_ID,
        "stores": stores,
        "skipped_undecodable": skipped,
        "truncated": truncated,
        "note": (
            "a MENU read from one node's view of the chain, not an authorization: the "
            "authorities listed here are what the store accounts SAY, and a purchase "
            "still verifies everything before anything signs. Every store here is "
            "buyable through prepare_purchase, which re-reads the one you name from its "
            "own account rather than trusting this list."
        ),
    }


def list_stores_result(
    arguments: Any, *, rpc_call: RpcCall | None = None
) -> dict[str, Any]:
    """The surface-facing entry: validate, resolve the RPC, list. Never raises for an answer.

    The same two rules ``prepare_purchase`` enforces, because this is served on the same
    unauthenticated door: the NETWORK is asserted by the caller (never inferred from a
    URL), and a caller-supplied ``rpc_url`` goes through the SSRF guard before anything is
    fetched. A transport failure comes back redacted to its class.
    """
    args = arguments or {}
    network = coerce_network(args.get("network"))
    if network == UNKNOWN_NETWORK:
        return {
            "error": (
                "say which network to list: mainnet, devnet, testnet or fork. Nothing "
                "here infers it, and 'unknown' lists nothing."
            )
        }
    rpc_url, refusal = _resolve_rpc_url(args.get("rpc_url"), network)
    if rpc_url is None:
        return {"error": refusal or "no usable rpc_url"}
    product = args.get("product")
    product = product if isinstance(product, str) else None
    try:
        out = list_stores(rpc_url=rpc_url, rpc_call=rpc_call, product=product)
    except RpcError as exc:
        # The failure class only — an RPC error body is untrusted transport output.
        return {"error": f"RpcError: {exc}"}
    out["network"] = network
    return out


LIST_STORES_TOOL: dict[str, Any] = {
    "name": "list_stores",
    "description": (
        "List every let_me_buy storefront on the network you name — store names, "
        "products and prices, read from each store's own on-chain account. Optionally "
        "filter by product ('water' finds 'Water' and 'Sparkling water'). This is a MENU, "
        "not an authorization: the directory reports what the accounts say, accounts "
        "that do not decode are counted rather than guessed at, and a purchase still "
        "verifies everything before anything signs. Read-only; nothing here holds a key."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "network": {
                "type": "string",
                "enum": ["mainnet", "devnet", "testnet", "fork"],
                "description": (
                    "which network to list. YOU say; nothing here infers it from an "
                    "RPC URL, and there is no default."
                ),
            },
            "product": {
                "type": "string",
                "description": "optional case-insensitive product filter, e.g. 'water'",
            },
            "rpc_url": {
                "type": "string",
                "description": (
                    "the http(s) RPC to read from; defaults to the public endpoint for "
                    "the network you named. Required for a fork."
                ),
            },
        },
        "required": ["network"],
        "additionalProperties": False,
    },
}
