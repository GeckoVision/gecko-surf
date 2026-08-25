"""A store NAME must resolve to that store's OWN accounts — pinned against mainnet facts.

The failure being fenced off is measured, not imagined: on mainnet a run named
``geckocoffee`` in the instruction while passing jonasbar's ``receipts``, and the program
refused it with ``ConstraintSeeds`` (2006) — left ``H7Bj…`` (passed), right ``HVkb…``
(derived from the name). The chain caught it. Nothing before the chain did.

Two properties carry this file:

1. **The derivation replaces the constants FAITHFULLY, not plausibly.** jonasbar's
   receipts PDA and token account are asserted to equal the exact strings that used to be
   hardcoded in both scripts. A resolver that returned "some plausible address" would pass
   a shape check and fail this.
2. **A half-selected store cannot be built.** :class:`StoreAccounts` re-derives what it
   can at construction, so pairing one store's name with another's receipts raises offline.

The transport is injected — no network, and the account bytes are built by the same
IDL-shaped encoder ``tests/test_store_directory.py`` uses, so the decoder is exercised
against genuinely-shaped bytes rather than a mock of itself.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from gecko.store_accounts import (
    ProductNotOnMenu,
    StoreAccounts,
    StoreAccountsMismatch,
    StoreNotFound,
    StoreResolutionError,
    allowed_destinations,
    derive_ata,
    purchase_accounts,
    purchase_args,
    receipts_pda,
    resolve_store,
)
from gecko.store_directory import LET_ME_BUY_PROGRAM_ID, StoreProduct
from test_store_directory import encode_store

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
#: let_me_buy pins this in the IDL, so every ATA on its path derives under it.
CLASSIC_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

#: Measured on mainnet, and the exact strings ``scripts/prepare_purchase.py`` and
#: ``scripts/autonomous_purchase.py`` used to hardcode as STORE_RECEIPTS / STORE_AUTHORITY
#: / STORE_TOKEN_ACCOUNT. They are the ground truth the derivation has to reproduce.
JONASBAR_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"
JONASBAR_AUTHORITY = "8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V"
JONASBAR_TOKEN_ACCOUNT = "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY"

#: Also measured on mainnet: the receipts PDA the program itself derived for this name
#: (the "Right:" side of the ConstraintSeeds error), the authority written in that
#: account, and that authority's USDC token account.
GECKOCOFFEE_RECEIPTS = "HVkbYf9PBF49WVViFf7eM1VescsgRHNeu4XJv1XveC8x"
GECKOCOFFEE_AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
GECKOCOFFEE_TOKEN_ACCOUNT = "AzNx1xhhXNAWueYhuusvzYessN23RNXk1xfmU7iJ5rjB"

BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"

#: The live menus, for fixtures.
MENUS: dict[str, list[tuple[str, int, int]]] = {
    "jonasbar": [("Water", 100_000, 6), ("Jägermeister", 500_000, 6)],
    "geckocoffee": [("Espresso", 100_000, 6), ("Sparkling water", 50_000, 6)],
    "alexsbar": [("Wine", 200_000, 6)],
}

#: Each store's OWN merchant. Two are the measured mainnet authorities; alexsbar's is a
#: fixture-only pubkey — what it must not be is a copy of another store's.
AUTHORITIES: dict[str, str] = {
    "jonasbar": JONASBAR_AUTHORITY,
    "geckocoffee": GECKOCOFFEE_AUTHORITY,
    "alexsbar": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
}


class FakeRpc:
    """One node's answers, and a record of what was asked. No network, no mocking library."""

    def __init__(self, accounts: dict[str, dict[str, Any]]) -> None:
        self.accounts = accounts
        self.calls: list[tuple[str, list[Any]]] = []

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method != "getAccountInfo":  # pragma: no cover - the resolver asks one thing
            raise AssertionError(f"unexpected RPC method {method}")
        return {"result": {"value": self.accounts.get(str(params[0]))}}


def account(raw: bytes, *, owner: str = LET_ME_BUY_PROGRAM_ID) -> dict[str, Any]:
    return {"owner": owner, "data": [base64.b64encode(raw).decode(), "base64"]}


def node_with(*names: str) -> FakeRpc:
    """A node serving each named store at the address its own name derives."""
    return FakeRpc(
        {
            receipts_pda(name): account(
                encode_store(name, products=MENUS[name], authority=AUTHORITIES[name])
            )
            for name in names
        }
    )


def jonasbar_accounts() -> StoreAccounts:
    """jonasbar as the resolver hands it over — the real dataclass, so its guard runs."""
    return StoreAccounts(
        store_name="jonasbar",
        product=StoreProduct(name="Water", price_raw=100_000, decimals=6, mint=USDC),
        receipts=JONASBAR_RECEIPTS,
        authority=JONASBAR_AUTHORITY,
        token_account=JONASBAR_TOKEN_ACCOUNT,
    )


# -- the pinned fixture: the derivation reproduces the constants it replaces -----------


def test_jonasbars_derived_accounts_equal_the_constants_they_replace() -> None:
    """The whole licence to delete six hardcoded addresses.

    Both scripts pinned these three strings. If the derivation returns anything else, the
    replacement was plausible rather than faithful — and a plausible payee is a wrong one.
    """
    assert receipts_pda("jonasbar") == JONASBAR_RECEIPTS
    assert derive_ata(JONASBAR_AUTHORITY, USDC, token_program=CLASSIC_TOKEN) == JONASBAR_TOKEN_ACCOUNT


def test_the_default_store_still_resolves_to_exactly_what_it_hardcoded() -> None:
    rpc = node_with("jonasbar")
    store = resolve_store("jonasbar", rpc_url="http://node", rpc_call=rpc).accounts_for(
        "Water"
    )
    assert (store.receipts, store.authority, store.token_account) == (
        JONASBAR_RECEIPTS,
        JONASBAR_AUTHORITY,
        JONASBAR_TOKEN_ACCOUNT,
    )
    # One targeted read, at the address the NAME derives — not a program-wide scan.
    assert [method for method, _ in rpc.calls] == ["getAccountInfo"]
    assert rpc.calls[0][1][0] == JONASBAR_RECEIPTS


# -- a store that is NOT the default gets its own accounts, everywhere ------------------


def test_a_named_store_resolves_to_its_own_accounts_and_they_reach_the_plan() -> None:
    """The regression anchor, at package level: name geckocoffee, pay geckocoffee."""
    store = resolve_store(
        "geckocoffee", rpc_url="http://node", rpc_call=node_with("geckocoffee")
    ).accounts_for("Espresso")

    assert store.receipts == GECKOCOFFEE_RECEIPTS
    assert store.authority == GECKOCOFFEE_AUTHORITY
    assert store.token_account == GECKOCOFFEE_TOKEN_ACCOUNT
    # Not jonasbar's, in any slot.
    assert JONASBAR_RECEIPTS not in (store.receipts, store.authority)
    assert store.token_account != JONASBAR_TOKEN_ACCOUNT

    accounts = purchase_accounts(store, buyer=BUYER)
    assert accounts["receipts"] == GECKOCOFFEE_RECEIPTS
    assert accounts["authority"] == GECKOCOFFEE_AUTHORITY
    assert accounts["recipient_token_account"] == GECKOCOFFEE_TOKEN_ACCOUNT
    assert accounts["sender_token_account"] == derive_ata(BUYER, USDC, token_program=CLASSIC_TOKEN)
    # The instruction argument and the accounts come from ONE object, so they agree.
    assert purchase_args(store, table=3)["store_name"] == "geckocoffee"
    assert allowed_destinations(store, buyer_ata=accounts["sender_token_account"]) == {
        GECKOCOFFEE_RECEIPTS,
        GECKOCOFFEE_AUTHORITY,
        GECKOCOFFEE_TOKEN_ACCOUNT,
        accounts["sender_token_account"],
    }


def test_each_store_on_the_same_node_gets_its_own_addresses() -> None:
    """Three stores, one node: nothing leaks between them."""
    rpc = node_with("geckocoffee", "alexsbar")
    espresso = resolve_store(
        "geckocoffee", rpc_url="http://node", rpc_call=rpc
    ).accounts_for("Espresso")
    wine = resolve_store("alexsbar", rpc_url="http://node", rpc_call=rpc).accounts_for(
        "Wine"
    )
    assert espresso.receipts != wine.receipts
    assert espresso.product.price_ui == "0.1"
    assert wine.product.price_ui == "0.2"


# -- the mismatch guard: a half-selected store cannot be constructed --------------------


def test_pairing_one_stores_name_with_another_stores_receipts_is_refused() -> None:
    """The mainnet fault, expressed directly — and refused offline instead of on chain."""
    with pytest.raises(StoreAccountsMismatch) as refusal:
        StoreAccounts(
            store_name="geckocoffee",
            product=StoreProduct(
                name="Espresso", price_raw=100_000, decimals=6, mint=USDC
            ),
            receipts=JONASBAR_RECEIPTS,  # the address the live run actually passed
            authority=GECKOCOFFEE_AUTHORITY,
            token_account=GECKOCOFFEE_TOKEN_ACCOUNT,
        )
    # Both sides named, exactly as the program's own error names them.
    assert JONASBAR_RECEIPTS in str(refusal.value)
    assert GECKOCOFFEE_RECEIPTS in str(refusal.value)


def test_a_token_account_that_is_not_the_authoritys_is_refused() -> None:
    """Paying a token account owned by someone else is the same class of fault."""
    with pytest.raises(StoreAccountsMismatch) as refusal:
        StoreAccounts(
            store_name="geckocoffee",
            product=StoreProduct(
                name="Espresso", price_raw=100_000, decimals=6, mint=USDC
            ),
            receipts=GECKOCOFFEE_RECEIPTS,
            authority=GECKOCOFFEE_AUTHORITY,
            token_account=JONASBAR_TOKEN_ACCOUNT,
        )
    assert GECKOCOFFEE_TOKEN_ACCOUNT in str(refusal.value)


# -- the two refusals ------------------------------------------------------------------


def test_an_unknown_store_refuses_by_name_and_never_falls_back() -> None:
    rpc = node_with("geckocoffee")
    with pytest.raises(StoreNotFound) as refusal:
        resolve_store("nosuchbar", rpc_url="http://node", rpc_call=rpc)
    message = str(refusal.value)
    assert "nosuchbar" in message
    assert receipts_pda("nosuchbar") in message
    # It must not quietly answer with the store that DOES exist on this node.
    assert GECKOCOFFEE_RECEIPTS not in message


def test_a_product_not_on_the_menu_refuses_and_says_what_is() -> None:
    store = resolve_store(
        "geckocoffee", rpc_url="http://node", rpc_call=node_with("geckocoffee")
    )
    with pytest.raises(ProductNotOnMenu) as refusal:
        store.accounts_for("Jägermeister")
    message = str(refusal.value)
    assert "Jägermeister" in message and "geckocoffee" in message
    assert "Espresso" in message and "Sparkling water" in message


def test_the_product_match_is_case_insensitive_but_still_exact() -> None:
    store = resolve_store(
        "geckocoffee", rpc_url="http://node", rpc_call=node_with("geckocoffee")
    )
    assert store.accounts_for("espresso").product.name == "Espresso"
    # "water" is a SUBSTRING of "Sparkling water" — a menu lookup is not a search.
    with pytest.raises(ProductNotOnMenu):
        store.accounts_for("water")


# -- the node is not believed ----------------------------------------------------------


def test_an_account_owned_by_another_program_is_not_a_storefront() -> None:
    rpc = FakeRpc(
        {
            receipts_pda("geckocoffee"): account(
                encode_store("geckocoffee", products=MENUS["geckocoffee"]),
                owner="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            )
        }
    )
    with pytest.raises(StoreNotFound, match="not the let_me_buy program"):
        resolve_store("geckocoffee", rpc_url="http://node", rpc_call=rpc)


def test_a_node_answering_with_a_different_stores_bytes_is_refused() -> None:
    """The address was derived from the name, so a different name inside it is a lie."""
    rpc = FakeRpc(
        {
            receipts_pda("geckocoffee"): account(
                encode_store("jonasbar", products=MENUS["jonasbar"])
            )
        }
    )
    with pytest.raises(StoreAccountsMismatch) as refusal:
        resolve_store("geckocoffee", rpc_url="http://node", rpc_call=rpc)
    assert "jonasbar" in str(refusal.value) and "geckocoffee" in str(refusal.value)


def test_bytes_that_do_not_decode_refuse_rather_than_guess() -> None:
    rpc = FakeRpc({receipts_pda("geckocoffee"): account(b"\xff" * 64)})
    with pytest.raises(StoreResolutionError, match="did not decode"):
        resolve_store("geckocoffee", rpc_url="http://node", rpc_call=rpc)


def test_an_empty_name_resolves_to_nothing_rather_than_to_a_default() -> None:
    rpc = node_with("jonasbar")
    with pytest.raises(StoreNotFound):
        resolve_store("   ", rpc_url="http://node", rpc_call=rpc)
    assert rpc.calls == []  # nothing was even asked


# -- the self-transfer seam stays usable -----------------------------------------------


def test_an_explicit_recipient_overrides_the_stores_token_account() -> None:
    """``--self-transfer`` binds the buyer's own account so the plan refusal can fire."""
    store = jonasbar_accounts()
    buyer_ata = derive_ata(BUYER, USDC, token_program=CLASSIC_TOKEN)
    accounts = purchase_accounts(store, buyer=BUYER, recipient=buyer_ata)
    assert accounts["recipient_token_account"] == buyer_ata
    assert accounts["sender_token_account"] == buyer_ata  # the collision, on purpose
    assert accounts["receipts"] == JONASBAR_RECEIPTS  # still this store's
