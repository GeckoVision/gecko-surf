"""The identity binding: whose wallet is the buyer, and who gets to say so.

The defect this file closes: ``prepare_purchase_result`` read the buyer from caller input.
That is CORRECT while the surface is keyless — you are preparing bytes for someone else to
sign, so they say who they are. Under a hosted signer (roadmap 2026-08-13, item (B)) it is
wrong: a caller could name an address they do not own.

So the binding is the FIRST line and it is fail-closed:

1. a bound account that supplies a DIFFERENT buyer is REFUSED — never silently
   substituted, because a silent substitution hands back a receipt describing a
   transaction the caller did not ask for;
2. a bound account that supplies NO buyer gets its bound wallet;
3. a bound account that supplies a MATCHING buyer proceeds;
4. an UNBOUND caller is mode A, unchanged — the caller-supplied buyer is used;
5. a directory that cannot answer refuses; it never degrades into mode A.

This file adds NO signing path and asserts none: the binding is an identity record and
the module under test still holds no key.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.prepare_purchase import prepare_purchase_result, prepare_purchase_tool
from gecko.providers.catalog_surface import OrquestraCatalogSurface
from gecko.wallet_binding import (
    InMemoryWalletDirectory,
    WalletBinding,
    WalletBindingError,
    WalletDirectory,
    binding_from_document,
)

from tests.test_prepare_purchase_tool import (
    BUYER,
    RPC_URL,
    FakeBuilder,
    FakeRpc,
)

ACCOUNT = "did:privy:cm5t0account0id"
WALLET_ID = "hqp2z8kwallet0id"
#: A second, real-shaped address the caller does NOT own — ``sha256`` of a fixed phrase,
#: so it belongs to nobody and is unrelated to the store, the mint, or any wired program.
#: Distinct from ``BUYER`` so a substitution shows up in the derived plan, and NOT the
#: store authority, so a refusal here can only be the binding check and never the
#: self-paying plan refusal wearing its coat.
OTHER_PUBKEY = "55CyNqUaSn4BQckUcx7R1DvFMTPXrKpGL7CBdVcb68zi"


def _bound(pubkey: str = BUYER) -> InMemoryWalletDirectory:
    directory = InMemoryWalletDirectory()
    directory.bind(
        WalletBinding(account_id=ACCOUNT, wallet_id=WALLET_ID, pubkey=pubkey)
    )
    return directory


class _UnreachableDirectory:
    """A directory that cannot answer — the Mongo-is-down case, without Mongo."""

    def wallet_for(self, account_id: str) -> WalletBinding | None:
        raise WalletBindingError("the wallet directory could not be read")


def _prepare(
    builder: FakeBuilder | None = None,
    rpc: FakeRpc | None = None,
    *,
    account: str | None = None,
    wallets: Any = None,
    drop_buyer: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "store": "jonasbar",
        "product": "Water",
        "buyer": BUYER,
        "network": "mainnet",
        "rpc_url": RPC_URL,
        "table": 11,
    }
    args.update(overrides)
    if drop_buyer:
        args.pop("buyer", None)
    return prepare_purchase_result(
        args,
        build_call=builder or FakeBuilder(),
        rpc_call=rpc or FakeRpc(),
        account=account,
        wallets=wallets,
    )


def _signer_of(result: dict[str, Any]) -> str:
    return next(e["address"] for e in result["accounts"] if e["account"] == "signer")


# --- 1. the regression: a bound account may not name someone else's address ------


def test_a_bound_account_naming_a_different_buyer_is_refused() -> None:
    builder, rpc = FakeBuilder(), FakeRpc()
    out = _prepare(builder, rpc, account=ACCOUNT, wallets=_bound(), buyer=OTHER_PUBKEY)

    assert out["refused"] is True
    assert out["code"] == "buyer-not-bound"
    assert "transaction" not in out
    # Refused BEFORE anything is built or fetched — the mismatch is decided on the record,
    # not on the builder's opinion of the plan.
    assert builder.calls == [], "the mismatch reached the builder"
    assert rpc.calls == [], "the mismatch reached the network"


def test_the_refusal_names_both_roles_and_echoes_neither_address() -> None:
    out = _prepare(account=ACCOUNT, wallets=_bound(), buyer=OTHER_PUBKEY)
    reason = out["reason"]

    # Both ROLES are named, so the caller can act on it...
    assert "buyer" in reason
    assert "bound" in reason
    # ...and NEITHER value appears: not the address the caller supplied (echoing untrusted
    # input back is how a refusal becomes a reflection gadget), and not the bound wallet
    # (a refusal must not be an oracle for the address behind an account id).
    whole = str(out)
    assert OTHER_PUBKEY not in whole
    assert BUYER not in whole
    assert ACCOUNT not in whole


# --- 2. a bound account, resolved from the session ------------------------------


def test_a_bound_account_with_no_buyer_uses_the_bound_wallet() -> None:
    builder, rpc = FakeBuilder(), FakeRpc()
    out = _prepare(builder, rpc, account=ACCOUNT, wallets=_bound(), drop_buyer=True)

    assert out["refused"] is False
    assert out["fee_payer"] == BUYER
    assert _signer_of(out) == BUYER
    # The bound pubkey reached the BUILDER as the fee payer, not just the response.
    assert builder.calls[0]["feePayer"] == BUYER
    assert builder.calls[0]["accounts"]["signer"] == BUYER


def test_a_bound_account_supplying_the_matching_buyer_proceeds() -> None:
    out = _prepare(account=ACCOUNT, wallets=_bound(), buyer=BUYER)
    assert out["refused"] is False
    assert _signer_of(out) == BUYER


def test_a_bound_account_supplying_an_unparseable_buyer_is_refused() -> None:
    # Not silently ignored in favour of the binding: the caller asserted something and it
    # was not an address, which is an argument error whatever the account is bound to.
    out = _prepare(account=ACCOUNT, wallets=_bound(), buyer="not-a-pubkey")
    assert out["refused"] is True
    assert out["code"] == "argument-invalid"


# --- 3. mode A is untouched -----------------------------------------------------


def test_no_account_and_no_directory_uses_the_caller_supplied_buyer() -> None:
    out = _prepare(buyer=BUYER)
    assert out["refused"] is False
    assert _signer_of(out) == BUYER


def test_a_directory_with_no_binding_for_this_account_stays_mode_a() -> None:
    # An enabled account that has enrolled no wallet is exactly the bring-your-own-custody
    # configuration; it must not be locked out of preparing bytes for its own wallet.
    out = _prepare(account="did:privy:someone-else", wallets=_bound(), buyer=BUYER)
    assert out["refused"] is False
    assert _signer_of(out) == BUYER


def test_an_account_with_no_directory_stays_mode_a() -> None:
    out = _prepare(account=ACCOUNT, wallets=None, buyer=BUYER)
    assert out["refused"] is False
    assert _signer_of(out) == BUYER


def test_mode_a_without_a_buyer_hands_back_a_signer_rather_than_a_schema_wall() -> None:
    # `buyer` left the required list, so this call now REACHES the tool. It must still be
    # impossible to buy without one — what changed is that the refusal carries the way
    # out, as DATA. A client that loads tools by search summarizes long descriptions; it
    # cannot summarize a tool result, which is the only reason this lives here.
    builder, rpc = FakeBuilder(), FakeRpc()
    out = _prepare(builder, rpc, drop_buyer=True, product="water")

    assert out["refused"] is True
    assert out["code"] == "signer-required"
    assert out["blocker_kind"] == "signer"
    assert "transaction" not in out
    # Nothing was built: no bytes exist, so no clock started. The node WAS asked, once,
    # for the store — that read is what lets the refusal name a resolved order.
    assert builder.calls == []
    assert rpc.calls == ["getAccountInfo"]

    # The order echo: a keyless agent learns its product name is right BEFORE it goes off
    # to install, fund and approval-settle a signer (blind-agent test, 2026-09-01).
    order = out["order"]
    assert order["order_valid"] is True
    assert order["store"] == "jonasbar"
    assert order["product"] == "Water"  # as listed on chain, not as the caller typed it
    assert order["price_ui"] == "0.1"
    assert order["network"] == "mainnet"
    assert "mint" in order
    assert out["reason"].startswith(
        "Order valid: Water at 0.1 from jonasbar on mainnet."
    )

    connectors = [signer["connector"] for signer in out["signers"]]
    assert "https://api.paybox.sh/mcp" in connectors, connectors
    reason = out["reason"].lower()
    # The ORDER is the load-bearing part — funding after the address, before re-calling.
    assert reason.index("ask it for") < reason.index("fund that address")
    assert "call this again" in reason
    # The keyless call builds nothing, so it must say no clock started (blind agent,
    # run 2: the old text implied the 60-second clock was already running).
    assert "no clock has started" in reason


def test_a_keyless_caller_with_a_wrong_product_learns_that_first() -> None:
    # The whole point of resolving the order before the signer check: a typo surfaces as
    # `product-unknown` with the menu, not as `signer-required` followed by a second
    # refusal after the wallet is funded.
    builder, rpc = FakeBuilder(), FakeRpc()
    out = _prepare(builder, rpc, drop_buyer=True, product="Sparkling wter")

    assert out["refused"] is True
    assert out["code"] == "product-unknown"
    assert "signers" not in out
    assert [entry["name"] for entry in out["products"]] == ["Water"]
    assert builder.calls == []


def test_a_keyless_caller_with_an_unknown_store_learns_that_first() -> None:
    builder, rpc = FakeBuilder(), FakeRpc(serve_store=False)
    out = _prepare(builder, rpc, drop_buyer=True)

    assert out["refused"] is True
    assert out["code"] == "store-unknown"
    assert "signers" not in out
    assert builder.calls == []


# --- 4. a directory that cannot answer fails CLOSED -----------------------------


def test_a_directory_that_cannot_answer_refuses_rather_than_degrading() -> None:
    builder, rpc = FakeBuilder(), FakeRpc()
    out = _prepare(
        builder,
        rpc,
        account=ACCOUNT,
        wallets=_UnreachableDirectory(),
        buyer=OTHER_PUBKEY,
    )
    assert out["refused"] is True
    assert out["code"] == "wallet-directory-unavailable"
    assert "transaction" not in out
    assert builder.calls == [] and rpc.calls == []


# --- 5. the tool schema tells the truth in both modes ---------------------------


def test_neither_schema_requires_a_buyer_but_they_mean_different_things_by_it() -> None:
    # `buyer` is optional in BOTH modes now, for two DIFFERENT reasons, so the schemas
    # agree on the key and differ on its meaning. Unbound: optional so that a caller with
    # no wallet reaches the `signer-required` refusal (which names the signers that reach
    # its client) instead of bouncing off the schema and asking a human for a base58
    # string. Bound: optional because the server looks it up.
    unbound = prepare_purchase_tool(buyer_bound=False)
    assert unbound["inputSchema"]["required"] == ["store", "product"]
    unbound_described = unbound["inputSchema"]["properties"]["buyer"][
        "description"
    ].lower()
    assert "signer" in unbound_described
    assert "omit" in unbound_described

    bound = prepare_purchase_tool(buyer_bound=True)
    assert "buyer" not in bound["inputSchema"]["required"]
    described = bound["inputSchema"]["properties"]["buyer"]["description"].lower()
    # The agent must be able to READ the rule the server enforces, not discover it by
    # being refused: omitting is fine, mismatching is a refusal.
    assert "omit" in described
    assert "refus" in described


def test_the_surface_serves_the_bound_schema_only_when_a_directory_is_wired() -> None:
    # The DESCRIPTION is what the binding changes now, not the required list.
    plain = OrquestraCatalogSurface()
    tool = next(t for t in plain.list_tools() if t["name"] == "prepare_purchase")
    assert (
        "bound wallet" not in tool["inputSchema"]["properties"]["buyer"]["description"]
    )

    hosted = OrquestraCatalogSurface(wallets=_bound())
    tool = next(t for t in hosted.list_tools() if t["name"] == "prepare_purchase")
    assert "bound wallet" in tool["inputSchema"]["properties"]["buyer"]["description"]


def test_the_surface_passes_the_account_through_to_the_binding() -> None:
    surface = OrquestraCatalogSurface(
        purchase_build_call=FakeBuilder(),
        purchase_rpc_call=FakeRpc(),
        wallets=_bound(),
    )
    args = {
        "store": "jonasbar",
        "product": "Water",
        "buyer": OTHER_PUBKEY,
        "network": "mainnet",
        "rpc_url": RPC_URL,
    }
    assert surface.call_tool("prepare_purchase", args, account=ACCOUNT)["code"] == (
        "buyer-not-bound"
    )
    # ...and with no account the same call is mode A and proceeds on the caller's word.
    assert surface.call_tool("prepare_purchase", dict(args, buyer=BUYER))[
        "refused"
    ] is (False)


# --- 6. the record itself -------------------------------------------------------


def test_the_binding_carries_ids_and_a_pubkey_and_nothing_else() -> None:
    binding = WalletBinding(account_id=ACCOUNT, wallet_id=WALLET_ID, pubkey=BUYER)
    assert set(vars(binding)) == {"account_id", "wallet_id", "pubkey"}
    # Founder ruling: the email is for COMMUNICATION and is not on the signing path.
    assert not any("mail" in name for name in vars(binding))
    with pytest.raises(AttributeError):
        binding.pubkey = OTHER_PUBKEY  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": "", "wallet_id": WALLET_ID, "pubkey": BUYER},
        {"account_id": ACCOUNT, "wallet_id": "  ", "pubkey": BUYER},
        {"account_id": ACCOUNT, "wallet_id": WALLET_ID, "pubkey": ""},
        {"account_id": ACCOUNT, "wallet_id": WALLET_ID, "pubkey": "too-short"},
        {"account_id": ACCOUNT, "wallet_id": WALLET_ID, "pubkey": "0" * 40},
        {"account_id": ACCOUNT, "wallet_id": "a@example.com", "pubkey": BUYER},
    ],
)
def test_a_malformed_binding_is_refused_at_construction(kwargs: dict[str, str]) -> None:
    with pytest.raises(WalletBindingError):
        WalletBinding(**kwargs)


def test_the_in_memory_directory_answers_only_for_the_account_it_bound() -> None:
    directory = _bound()
    assert isinstance(directory, WalletDirectory)
    found = directory.wallet_for(ACCOUNT)
    assert found is not None and found.pubkey == BUYER
    assert directory.wallet_for("did:privy:nobody") is None
    assert directory.wallet_for("") is None


def test_a_stored_document_yields_ids_and_a_pubkey_and_drops_anything_else() -> None:
    binding = binding_from_document(
        {
            "account_id": ACCOUNT,
            "wallet_id": WALLET_ID,
            "pubkey": BUYER,
            "email": "someone@example.com",  # present in the row; never on this path
            "_id": "mongo-object-id",
        }
    )
    assert binding == WalletBinding(
        account_id=ACCOUNT, wallet_id=WALLET_ID, pubkey=BUYER
    )
    assert "example.com" not in repr(binding)


@pytest.mark.parametrize("missing", ["account_id", "wallet_id", "pubkey"])
def test_a_document_missing_any_field_raises_rather_than_binding_a_partial(
    missing: str,
) -> None:
    # Each field on its own: a default for ANY of the three would let a half-written row
    # answer "this is the wallet", which is the same defect as trusting the caller.
    row = {"account_id": ACCOUNT, "wallet_id": WALLET_ID, "pubkey": BUYER}
    del row[missing]
    with pytest.raises(WalletBindingError):
        binding_from_document(row)


def test_a_document_that_is_not_a_mapping_raises() -> None:
    with pytest.raises(WalletBindingError):
        binding_from_document(["not", "a", "mapping"])


# --- 7. the hosted directory fails CLOSED ---------------------------------------


class _ExplodingCollection:
    def find_one(self, _query: dict[str, Any]) -> dict[str, Any] | None:
        raise RuntimeError("mongodb://user:s3cret@cluster/gecko_registry unreachable")


class _DictCollection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (r for r in self.rows if r["account_id"] == query["account_id"]), None
        )


def test_the_mongo_directory_reads_a_binding_and_returns_none_when_absent() -> None:
    from gecko.registry.wallets import MongoWalletDirectory

    directory = MongoWalletDirectory(
        collection=_DictCollection(
            [{"account_id": ACCOUNT, "wallet_id": WALLET_ID, "pubkey": BUYER}]
        )
    )
    assert isinstance(directory, WalletDirectory)
    found = directory.wallet_for(ACCOUNT)
    assert found is not None and found.wallet_id == WALLET_ID
    assert directory.wallet_for("did:privy:nobody") is None


def test_the_mongo_directory_raises_on_a_driver_failure_and_redacts_the_uri() -> None:
    from gecko.registry.wallets import MongoWalletDirectory

    directory = MongoWalletDirectory(collection=_ExplodingCollection())
    with pytest.raises(WalletBindingError) as caught:
        directory.wallet_for(ACCOUNT)
    # A pymongo error can carry the connection URI, so the raised error names only the
    # failure class, chains no cause, and suppresses the original from any traceback.
    assert "s3cret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
