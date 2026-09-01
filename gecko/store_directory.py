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

from .tools import tool_annotations

import base64
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .networks import APPROVABLE_NETWORKS, UNKNOWN_NETWORK, coerce_network
from .prepare_purchase import USDC_MINT, _resolve_rpc_url, BLOCKHASH_CLOCK_PROSE
from .rpc import RpcCall, RpcError, default_rpc_call
from .token_program import (
    MintTokenProgram,
    read_mint_token_programs,
    unknown_token_program,
)

__all__ = [
    "LET_ME_BUY_PROGRAM_ID",
    "LIST_STORES_TOOL",
    "StoreDecodeError",
    "StoreListing",
    "StoreProduct",
    "authority_span",
    "decode_store",
    "encode_store",
    "list_stores",
    "list_stores_result",
    "receipts_discriminator",
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

    @property
    def position(self) -> int:
        """How many bytes have been consumed — i.e. the offset of the NEXT field.

        Exposed because one caller needs a field's byte RANGE rather than its value
        (:func:`authority_span`), and the alternative is a second copy of this layout's
        arithmetic in another module. Read-only: nothing can seek this cursor.
        """
        return self._position

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
    #: Where the program tells a fulfiller to send the order. `PurchaseMade` carries this
    #: value, so it is the delivery ADDRESS, not a label — and an empty one means a
    #: purchase is recorded and nobody is told to make it. Surfaced so a buyer can see
    #: that before paying rather than after.
    #:
    #: WE CANNOT VERIFY IT RESOLVES. Empty is checkable from chain; wrong-but-present is
    #: not, and looks identical to correct. Say so wherever this is shown.
    telegram_channel_id: str = ""


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
    # The fulfilment channel sits after the products vec. Read defensively: an older
    # account laid out before this field existed simply ends here, and a truncated read is
    # reported as absent rather than raising — a missing channel is a fact about the store,
    # not a reason to drop the whole storefront from the menu.
    try:
        telegram_channel_id = cursor.string()
    except StoreDecodeError:
        telegram_channel_id = ""
    return StoreListing(
        store_name=store_name,
        address=address,
        authority=authority,
        total_purchases=total_purchases,
        products=tuple(products),
        telegram_channel_id=telegram_channel_id,
    )


def receipts_discriminator() -> bytes:
    """Anchor's 8-byte account discriminator for the ``Receipts`` account.

    ``sha256("account:Receipts")[:8]`` — the derivation Anchor itself uses, and
    verified byte-for-byte against the live geckocoffee account
    (``def5ed403b311df6``). Computed rather than hardcoded so it stays honest if
    the derivation is ever the thing in question; a wrong discriminator is
    skipped on READ (``decode_store`` starts past it) but REJECTED by the program
    on a real purchase, so a store seeded with the wrong one reads fine and
    cannot be bought from — the exact trap this function exists to avoid.
    """
    import hashlib

    return hashlib.sha256(b"account:Receipts").digest()[:8]


def _encode_string(text: str) -> bytes:
    body = text.encode("utf-8")
    return len(body).to_bytes(4, "little") + body


def _encode_pubkey(address: str) -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(address))


def encode_store(
    listing: StoreListing,
    *,
    discriminator: bytes | None = None,
    receipt_rows: int = 0,
) -> bytes:
    """Encode a :class:`StoreListing` into ``Receipts`` account bytes.

    The faithful inverse of :func:`decode_store`, in the program IDL's own
    layout. Used to SEED a store on a fork through the setAccount cheatcode: the
    bytes this returns, written at ``receipts_pda(name)`` with the program as
    owner, are a store buyers can list and buy from.

    ``discriminator`` defaults to :func:`receipts_discriminator` — the real
    Anchor bytes, so a seeded store is BUYABLE, not merely readable. ``receipt_rows``
    seeds an EMPTY receipts vec by default (a fresh store has sold nothing);
    seeding rows is not supported because a receipt carries a real buyer and
    seeding fictional buyers is exactly the kind of fabricated state this repo
    refuses. ``total_purchases`` is taken from the listing.

    A round-trip (``decode_store(encode_store(x)) == x`` for the fields decode
    returns) is asserted in the tests; this function is only ever the writer.
    """
    if receipt_rows != 0:
        raise StoreDecodeError(
            "encode_store seeds an empty receipts vec only (rows carry real buyers)"
        )
    parts = [
        discriminator if discriminator is not None else receipts_discriminator(),
        (0).to_bytes(4, "little"),  # receipts vec: empty
        listing.total_purchases.to_bytes(8, "little"),
        _encode_string(listing.store_name),
        _encode_pubkey(listing.authority),
        len(listing.products).to_bytes(4, "little"),
    ]
    for product in listing.products:
        parts.append(product.price_raw.to_bytes(8, "little"))
        parts.append(bytes([product.decimals]))
        parts.append(_encode_pubkey(product.mint))
        parts.append(_encode_string(product.name))
    parts.append(_encode_string(listing.telegram_channel_id))
    return b"".join(parts)


def authority_span(raw: bytes) -> tuple[int, int]:
    """``(start, end)`` — the bytes the store's ``authority`` pubkey occupies.

    A second walk of the same layout, deliberately kept BESIDE :func:`decode_store` rather
    than in the module that wants it. The authority sits after a variable-length receipts
    vec and a variable-length store name, so its offset cannot be a constant; a walk that
    lived somewhere else would be a copy of this layout free to drift from it.

    The only caller is the fork sandbox, which rewrites those 32 bytes with a cheatcode so
    that a delivery can be rehearsed as the merchant (see
    :func:`gecko.sandbox.deliver.rehearse_delivery`). Nothing on a real chain can do that,
    and nothing here writes anything — this returns two integers.

    :raises StoreDecodeError: if the bytes are not this layout. Never guesses an offset.
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
    cursor.u64()  # total_purchases
    cursor.string()  # store_name
    start = cursor.position
    cursor.take(32)  # the authority itself — taken so the bounds check runs
    return start, cursor.position


def _product_fields(
    products: Sequence[StoreProduct],
    programs: Mapping[str, MintTokenProgram],
) -> list[dict[str, Any]]:
    """One menu line per product, including the token program that owns its mint.

    ``mint_note`` is a human LABEL and stays exactly as it was. ``token_program`` is the
    fact underneath it: two mints can both say "USDC" to a person and sit on different
    token programs, in which case a wallet holding one cannot pay with it for the other.
    The program is read from the mint account; when the read failed it says so rather than
    falling back to the common case.
    """
    return [
        {
            "name": entry.name,
            "price_raw": entry.price_raw,
            "decimals": entry.decimals,
            "price_ui": entry.price_ui,
            "mint": entry.mint,
            "mint_note": "USDC" if entry.mint == USDC_MINT else None,
            "token_program": programs.get(
                entry.mint,
                unknown_token_program(entry.mint, "this mint was not read"),
            ).field(),
        }
        for entry in products
    ]


def _mints_of(listings: Iterable[StoreListing]) -> list[str]:
    return [product.mint for listing in listings for product in listing.products]


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

    Each product also carries the ``token_program`` that owns its mint, READ from the mint
    account in one batched round trip per listing — never inferred from the label, the
    decimals or the address. Two mints can present the same human name ("USDC") on
    different token programs and be different assets; the label alone cannot show that.

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

    listings: list[StoreListing] = []
    skipped = 0
    for row in rows[:_MAX_ACCOUNTS]:
        try:
            account = row["account"]
            raw = base64.b64decode(account["data"][0])
            listings.append(decode_store(raw, address=str(row["pubkey"])))
        except (StoreDecodeError, KeyError, TypeError, ValueError, IndexError):
            # Not this layout, or not the shape a node answers with. Counted, not guessed.
            skipped += 1
            continue

    # ONE read per DISTINCT mint for the whole directory, before any filtering: a menu
    # prices many products in the same mint, and reading it per line item would be a round
    # trip per row. Nothing is kept past this call.
    programs = read_mint_token_programs(
        _mints_of(listings), rpc_url=rpc_url, rpc_call=call
    )

    stores: list[dict[str, Any]] = []
    for listing in listings:
        matched = [
            entry
            for entry in listing.products
            if needle is None or needle in entry.name.lower()
        ]
        # A filter miss on the PRODUCT is not a miss on the store. "coffee" returned
        # nothing while geckocoffee sold Espresso — the word was in the store's own name,
        # which is how a person got there. An empty result must mean "nothing on this
        # network", never "your word did not literally appear in a product name".
        match_kind = None
        selected: Sequence[StoreProduct]
        if needle is None:
            selected = matched
        elif matched:
            match_kind = "product"
            selected = matched
        elif needle in listing.store_name.lower():
            match_kind = "store_name"
            selected = listing.products
        else:
            # Still nothing. The widening must not become "everything matches", or the
            # filter stops carrying information.
            continue
        stores.append(
            {
                "store": listing.store_name,
                "address": listing.address,
                "authority": listing.authority,
                "total_purchases": listing.total_purchases,
                **({"match_kind": match_kind} if match_kind else {}),
                # WHERE AN ORDER GOES. `PurchaseMade` carries this, so it is the delivery
                # address rather than a label: empty means the purchase is recorded and
                # nobody is told to make it. Shown so a buyer can see that BEFORE paying.
                # We cannot verify it resolves — empty is checkable, wrong-but-present is
                # not — so the value is reported and never endorsed.
                "fulfilment": {
                    "telegram_channel_id": listing.telegram_channel_id,
                    "set": bool(listing.telegram_channel_id),
                },
                "products": _product_fields(selected, programs),
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
        "mint_note_caveat": (
            "`mint_note` is a human LABEL, and two different mints can wear the same one. "
            "Match on `mint` and `token_program`, never on the label: a token-2022 mint "
            "and a classic-spl-token mint are different assets even when both are called "
            "USDC-something, and a wallet holding one CANNOT pay with it where the other "
            "is priced. `token_program.read: false` means the mint could not be read and "
            "the program is unknown — it does not mean classic."
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
    # Same rule as `prepare_purchase`: a NODE without a chain is unguessable (a fork proxy
    # answers at any hostname), but browsing with neither can only mean mainnet. Requiring
    # it to READ a menu was the more absurd half of the old guard.
    network = coerce_network(args.get("network"))
    if network == UNKNOWN_NETWORK:
        supplied_url = args.get("rpc_url")
        if isinstance(supplied_url, str) and supplied_url.strip():
            return {
                "error": (
                    "you named an `rpc_url` but not a `network`. Which chain that node "
                    "answers for cannot be read from its hostname, so say mainnet, "
                    "devnet, testnet or fork. Omit BOTH and mainnet is assumed."
                )
            }
        network = coerce_network("mainnet")
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
    "annotations": tool_annotations(
        read_only=True, open_world=True, title="List stores and menus"
    ),
    "description": (
        "List every let_me_buy storefront on the network you name — store names, "
        "products and prices, read from each store's own on-chain account. Optionally "
        "filter by product ('water' finds 'Water' and 'Sparkling water'). This is a MENU, "
        "not an authorization: the directory reports what the accounts say, accounts "
        "that do not decode are counted rather than guessed at, and a purchase still "
        "verifies everything before anything signs. "
        "Each store reports its `fulfilment` channel — where the program tells a fulfiller "
        "to send the order. `set: false` means a purchase would be recorded and NOBODY "
        "told to make it; say so before someone pays. A value being present is not a "
        "promise it resolves — that cannot be checked from the chain. "
        "BROWSE HERE, NOT WITH `prepare_purchase`. This costs nothing and expires never, "
        "so show these prices, let the buyer choose, and only then call "
        f"`prepare_purchase` — which starts a {BLOCKHASH_CLOCK_PROSE} clock on a live blockhash. Each "
        "product carries `price_ui` for display and `price_raw` + `decimals` + `mint` for "
        "anything else; a store may price in a mint that is not USDC, so read the mint "
        "rather than assuming one. "
        "EACH PRODUCT ALSO CARRIES `token_program` — the program that owns its mint, read "
        "from the mint account. `mint_note` is only a human label, and two different mints "
        "can wear the same one: a `token-2022` mint and a `classic-spl-token` mint are "
        "DIFFERENT ASSETS even when both read as a dollar stablecoin, and a wallet holding "
        "one cannot pay with it where the other is priced. Check the buyer's holding "
        "against `mint` AND `token_program` before preparing. `token_program.read: false` "
        "means the mint could not be read, which is not the same as classic. "
        "Read-only; nothing here holds a key."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "network": {
                "type": "string",
                "enum": sorted(APPROVABLE_NETWORKS),
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
        "required": [],
        "additionalProperties": False,
    },
}
