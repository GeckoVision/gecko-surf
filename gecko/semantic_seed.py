"""Seed a store on a fork from the semantic catalogue — the dev-fast unblock.

The hard blocker for running the scenarios was a geckocoffee store carrying the
31-item confusable catalogue. Seeding it on MAINNET (real `initialize` +
`add_product`, founder-signed) is the production path; this module is the
FORK-ONLY path that makes tests move fast: encode the catalogue into
``Receipts`` account bytes and write them straight into a surfpool fork through
the sanctioned overlay cheatcode. No signature, no broadcast, no mainnet — a
local validator state edit that exists only on the fork.

What it seeds is a store buyers can list AND buy from: the real Anchor
discriminator (so the program accepts the account on a purchase), the program
as owner, rent-exempt lamports, and the products from ``to_store_config()``.
The product ATTRIBUTES (contains_coffee, temperature, …) do NOT go on chain —
the chain carries the menu (name, price, mint); the semantic catalogue carries
the meaning. That split is the whole point: the gate reads attributes from the
catalogue, prices from the store.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gecko.fork_preflight import AccountState, OverlayApply
from gecko.rpc import RpcCall, default_rpc_call
from gecko.sandbox.surfnet import SurfnetProof
from gecko.store_accounts import receipts_pda
from gecko.store_directory import (
    LET_ME_BUY_PROGRAM_ID,
    StoreDecodeError,
    StoreListing,
    StoreProduct,
    decode_store,
    encode_store,
)

#: USDC on Solana mainnet — the mint a fork inherits, and what geckocoffee prices in.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


class SeedError(Exception):
    """Raised when the fork will not accept the seeded store."""


def config_to_listing(
    config: Mapping[str, Any],
    *,
    authority: str,
    address: str,
    mint: str = USDC_MINT,
    decimals: int = USDC_DECIMALS,
    telegram_channel_id: str = "@geckocoffeeshop",
) -> StoreListing:
    """Map a ``to_store_config()`` dict into a :class:`StoreListing` to encode.

    ``price_raw`` is the catalogue's ``price_lamports`` verbatim, so the scenario
    budgets (set against those same numbers) and the price the fork surface reads
    back stay one number, never two.
    """
    products = tuple(
        StoreProduct(
            name=item["name"],
            price_raw=int(item["price_lamports"]),
            decimals=decimals,
            mint=mint,
        )
        for item in config["items"]
    )
    return StoreListing(
        store_name=str(config["store"]),
        address=address,
        authority=authority,
        total_purchases=0,
        products=products,
        telegram_channel_id=telegram_channel_id,
    )


def read_store_on_fork(
    address: str,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    *,
    program_id: str = LET_ME_BUY_PROGRAM_ID,
) -> StoreListing | None:
    """Read one store on a fork, robust to surfpool's setAccount/getAccountInfo lag.

    Measured on surfpool 1.1.1: ``surfnet_setAccount`` populates the program's
    account index (``getProgramAccounts`` sees the seeded store immediately) but
    ``getAccountInfo`` on that exact address returns ``null`` until a transaction
    materialises it — a read-view lag, not a state one (the account IS in fork
    state; a purchase's execution sees it). So try the direct read first, then
    fall back to the program-accounts scan the directory itself uses. Returns
    ``None`` when neither path finds a decodable store.
    """
    import base64

    call = rpc_call or default_rpc_call
    direct = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
    value = direct.get("value") if isinstance(direct, dict) else None
    if value:
        try:
            return decode_store(base64.b64decode(value["data"][0]), address=address)
        except StoreDecodeError:
            return None

    scan = call(rpc_url, "getProgramAccounts", [program_id, {"encoding": "base64"}])
    rows = scan.get("result") if isinstance(scan, dict) else None
    for row in rows or []:
        if row.get("pubkey") == address:
            try:
                return decode_store(
                    base64.b64decode(row["account"]["data"][0]), address=address
                )
            except StoreDecodeError:
                return None
    return None


def _min_rent(size: int, rpc_url: str, rpc_call: RpcCall) -> int:
    result = rpc_call(rpc_url, "getMinimumBalanceForRentExemption", [size])
    value = result.get("result") if isinstance(result, dict) else None
    if not isinstance(value, int):
        raise SeedError(
            "the fork did not return a rent-exemption figure for the store size"
        )
    return value


def seed_store(
    proof: SurfnetProof,
    overlay: OverlayApply,
    config: Mapping[str, Any],
    *,
    authority: str,
    mint: str = USDC_MINT,
    decimals: int = USDC_DECIMALS,
    rpc_call: RpcCall | None = None,
) -> str:
    """Write the store from ``config`` onto the fork. Returns its account address.

    ``proof`` is the surfnet attestation — it binds this to a proven fork and
    carries the ``rpc_url``. ``overlay`` is :func:`gecko.fork_preflight.surfnet_overlay`
    for that url (the one place in the repo that writes account data). ``authority``
    is the store's merchant/payee; on a fork it can be any pubkey (a buy credits
    ``ATA(authority, mint)``, created by the purchase), so pass a key you control
    if you want to read the payout back.
    """
    import base64

    call = rpc_call or default_rpc_call
    store_name = str(config["store"])
    address = receipts_pda(store_name)
    wanted = config_to_listing(
        config, authority=authority, address=address, mint=mint, decimals=decimals
    )
    raw = encode_store(wanted)
    lamports = _min_rent(len(raw), proof.rpc_url, call)

    state = AccountState(
        address=address,
        lamports=lamports,
        owner=LET_ME_BUY_PROGRAM_ID,
        data_base64=base64.b64encode(raw).decode("ascii"),
        executable=False,
    )
    overlay({address: state})

    # Read it back through the directory path (getProgramAccounts) — a seed that
    # does not decode there is a seed that did not take. getAccountInfo lags on
    # surfpool (see read_store_on_fork), so verifying against it would false-fail.
    listing = read_store_on_fork(address, proof.rpc_url, call)
    if listing is None:
        raise SeedError(
            f"store {store_name} did not decode at {address} after the overlay write"
        )
    if len(listing.products) != len(wanted.products):
        raise SeedError(
            f"store {store_name} read back {len(listing.products)} products, "
            f"seeded {len(wanted.products)}"
        )
    return address
