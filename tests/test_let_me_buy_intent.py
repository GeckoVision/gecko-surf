"""let_me_buy's servable plan intent — the buyer-side start, offline.

The plan resolves the store off the chain; here the read is faked with the SAME
dataclasses the real path constructs, so the derivation (receipts PDA, both ATAs from
the product's own mint) is proven with zero RPC — Pattern B.
"""

from __future__ import annotations


import gecko.providers.let_me_buy as let_me_buy
from gecko.providers.cli import PROGRAMS
from gecko.store_accounts import ResolvedStore, derive_ata, receipts_pda
from gecko.store_directory import StoreProduct
from gecko.landing import TOKEN_PROGRAM_ID

AUTHORITY = "APz2fu5vSHTM6wKTxRVEppseK9UHNhFEqfMFtRau1uk"
BUYER = "GDDMwNyyx8uB6zrqwBFHjLLG3TBYk2F8Az4yrQC5RzMp"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _fake_store(store_name: str = "geckocoffee") -> ResolvedStore:
    return ResolvedStore(
        store_name=store_name,
        receipts=receipts_pda(store_name),
        authority=AUTHORITY,
        products=(
            StoreProduct(name="espresso", price_raw=1_000_000, decimals=6, mint=USDC),
        ),
    )


def test_plan_purchase_derives_the_full_account_map(monkeypatch) -> None:
    monkeypatch.setattr(
        let_me_buy, "resolve_store", lambda name, *, rpc_url: _fake_store(name)
    )
    surface = PROGRAMS["let_me_buy"]()
    plan = surface.call_tool(
        "plan_purchase",
        {"store": "geckocoffee", "product": "espresso", "buyer": BUYER},
    )
    derived = plan["derived"]
    assert derived["receipts"] == receipts_pda("geckocoffee")
    assert derived["sender_token_account"] == derive_ata(
        BUYER, USDC, token_program=TOKEN_PROGRAM_ID
    )
    assert derived["recipient_token_account"] == derive_ata(
        AUTHORITY, USDC, token_program=TOKEN_PROGRAM_ID
    )
    assert derived["authority"] == AUTHORITY
    assert plan["instruction"] == "make_purchase"
    assert plan["execute"]["url"].endswith("/instructions/make_purchase/build")


def test_plan_purchase_refuses_an_unlisted_product(monkeypatch) -> None:
    monkeypatch.setattr(
        let_me_buy, "resolve_store", lambda name, *, rpc_url: _fake_store(name)
    )
    surface = PROGRAMS["let_me_buy"]()
    out = surface.call_tool(
        "plan_purchase",
        {"store": "geckocoffee", "product": "flat white", "buyer": BUYER},
    )
    assert "error" in out
    assert "espresso" in out["error"]  # the refusal names what IS listed
