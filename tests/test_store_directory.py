"""The store directory — a MENU decoded from hostile bytes, falsified offline.

Pattern B: the transport is injected, so every branch is provable with no network and no
chain. The encoder below builds ``Receipts`` accounts in the program IDL's own layout, so
the decoder is exercised against genuinely-shaped bytes rather than mocks of itself — and
the hostile cases (truncated account, implausible lengths, wrong layout) are the same
bytes, damaged deliberately.

The property that justifies the file: an account that does not decode is COUNTED AND
SKIPPED, never guessed at and never fatal to the directory — and buyer data (the receipts
vec) never enters the return value.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from gecko.rpc import RpcError
from gecko.store_directory import (
    LET_ME_BUY_PROGRAM_ID,
    LIST_STORES_TOOL,
    StoreDecodeError,
    decode_store,
    list_stores,
    list_stores_result,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
#: An IP literal — the public-URL guard checks it without a DNS lookup, so the SSRF guard
#: runs for real and no network is touched.
RPC_URL = "https://93.184.216.34/rpc"
BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(text: str) -> bytes:
    value = 0
    for char in text:
        value = value * 58 + _B58.index(char)
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw


def _string(text: str) -> bytes:
    encoded = text.encode()
    return len(encoded).to_bytes(4, "little") + encoded


def _receipt(product: str = "Water") -> bytes:
    """One Receipt row: id, buyer, delivered, price, timestamp, table, product."""
    return (
        (7).to_bytes(8, "little")
        + _b58decode(BUYER).rjust(32, b"\x00")
        + b"\x01"
        + (100_000).to_bytes(8, "little")
        + (1_754_900_000).to_bytes(8, "little")
        + b"\x03"
        + _string(product)
    )


def encode_store(
    name: str = "geckocoffee",
    *,
    products: list[tuple[str, int, int]] | None = None,
    receipts: int = 2,
    total: int = 2,
    authority: str = AUTHORITY,
    mint: str = USDC,
    telegram: str = "",
) -> bytes:
    """A Receipts account in the IDL's own layout, discriminator included.

    ``authority`` is a parameter because a store's merchant is a per-store fact:
    ``tests/test_store_accounts.py`` encodes several stores against one node and each must
    carry its own, or a test would "pass" while every store shared one payee.
    """
    listed = products if products is not None else [("Espresso", 100_000, 6)]
    body = receipts.to_bytes(4, "little") + b"".join(
        _receipt() for _ in range(receipts)
    )
    body += total.to_bytes(8, "little")
    body += _string(name)
    body += _b58decode(authority).rjust(32, b"\x00")
    body += len(listed).to_bytes(4, "little")
    for product_name, price, decimals in listed:
        body += price.to_bytes(8, "little")
        body += bytes([decimals])
        body += _b58decode(mint).rjust(32, b"\x00")
        body += _string(product_name)
    # The fulfilment channel follows the products vec — `PurchaseMade` carries it, so it is
    # where an order is actually sent.
    body += _string(telegram)
    return b"\x00" * 8 + body


def _rows(*accounts: tuple[str, bytes]) -> list[dict[str, Any]]:
    return [
        {
            "pubkey": pubkey,
            "account": {"data": [base64.b64encode(raw).decode(), "base64"]},
        }
        for pubkey, raw in accounts
    ]


class FakeRpc:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, list[Any]]] = []

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"result": self.rows}


# -- decoding ------------------------------------------------------------------


def test_a_store_decodes_to_its_menu() -> None:
    listing = decode_store(
        encode_store(
            products=[("Espresso", 100_000, 6), ("Sparkling water", 50_000, 6)]
        ),
        address="Addr111",
    )
    assert listing.store_name == "geckocoffee"
    assert listing.authority == AUTHORITY
    assert listing.total_purchases == 2
    assert [p.name for p in listing.products] == ["Espresso", "Sparkling water"]
    assert listing.products[0].price_ui == "0.1"
    assert listing.products[1].price_ui == "0.05"


def test_buyer_data_never_enters_the_return_value() -> None:
    """The receipts vec carries buyer pubkeys. The listing must not."""
    listing = decode_store(encode_store(receipts=3), address="Addr111")
    assert BUYER not in json.dumps(
        {
            "store": listing.store_name,
            "authority": listing.authority,
            "products": [p.name for p in listing.products],
        }
    )
    assert not hasattr(listing, "receipts")


def test_a_truncated_account_refuses_instead_of_wrapping() -> None:
    raw = encode_store()
    with pytest.raises(StoreDecodeError):
        decode_store(raw[: len(raw) // 2], address="Addr111")


def test_an_implausible_string_length_refuses() -> None:
    raw = bytearray(encode_store(receipts=0))
    # The store-name length prefix sits right after the discriminator, the empty receipts
    # vec (4 bytes) and total_purchases (8 bytes). Corrupt it to 2**31.
    offset = 8 + 4 + 8
    raw[offset : offset + 4] = (2**31).to_bytes(4, "little")
    with pytest.raises(StoreDecodeError, match="implausible"):
        decode_store(bytes(raw), address="Addr111")


def test_wrong_layout_bytes_refuse() -> None:
    with pytest.raises(StoreDecodeError):
        decode_store(b"\x00" * 40, address="Addr111")


# -- the directory ---------------------------------------------------------------


def test_undecodable_accounts_are_counted_and_skipped_never_fatal() -> None:
    rpc = FakeRpc(
        _rows(
            ("Good1111", encode_store("jonasbar", products=[("Water", 100_000, 6)])),
            ("Bad11111", b"\xff" * 64),
        )
    )
    out = list_stores(rpc_url="http://node", rpc_call=rpc)
    assert [s["store"] for s in out["stores"]] == ["jonasbar"]
    assert out["skipped_undecodable"] == 1
    assert out["truncated"] is False


def test_the_product_filter_matches_substring_case_insensitively() -> None:
    rpc = FakeRpc(
        _rows(
            ("A1", encode_store("jonasbar", products=[("Water", 100_000, 6)])),
            (
                "B1",
                encode_store(
                    "geckocoffee",
                    products=[("Espresso", 100_000, 6), ("Sparkling water", 50_000, 6)],
                ),
            ),
            ("C1", encode_store("alexsbar", products=[("Wine", 200_000, 6)])),
        )
    )
    out = list_stores(rpc_url="http://node", rpc_call=rpc, product="water")
    by_store = {s["store"]: [p["name"] for p in s["products"]] for s in out["stores"]}
    assert by_store == {"jonasbar": ["Water"], "geckocoffee": ["Sparkling water"]}
    # The filter is REPORTED: a blind agent (run 2, 2026-09-01) could not tell whether
    # the one product shown was the whole menu, nor what `skipped_undecodable` counted.
    assert out["product_filter"] == {
        "applied": True,
        "query": "water",
        "note": out["product_filter"]["note"],
    }
    assert "without `product`" in out["product_filter"]["note"]
    assert "counted" in out["skipped_undecodable_note"]
    unfiltered = list_stores(rpc_url="http://node", rpc_call=FakeRpc([]))
    assert unfiltered["product_filter"]["applied"] is False


def test_it_asks_for_the_right_program() -> None:
    rpc = FakeRpc([])
    list_stores(rpc_url="http://node", rpc_call=rpc)
    method, params = rpc.calls[0]
    assert method == "getProgramAccounts"
    assert params[0] == LET_ME_BUY_PROGRAM_ID


# -- the surface entry ------------------------------------------------------------


def test_browsing_needs_no_network_but_naming_a_NODE_does() -> None:
    """The guard, narrowed to where it earned its keep.

    `network` used to be required so nothing could infer mainnet from an RPC URL — a fork
    proxy answers at any hostname. True, and aimed at a developer testing a fork, while the
    toll was paid by somebody trying to read a menu. The dangerous confusion is a fork
    MISTAKEN FOR mainnet, and defaulting to mainnet cannot cause that.

    So: name a node and you must name the chain it speaks for; name neither and it is
    mainnet, which is the only thing "what's on the menu" could mean.
    """
    listed = list_stores_result(
        {}, rpc_call=FakeRpc(_rows(("A1", encode_store("geckocoffee"))))
    )
    assert "error" not in listed
    assert listed["network"] == "mainnet"

    named_a_node = list_stores_result({"rpc_url": RPC_URL}, rpc_call=FakeRpc([]))
    assert "error" in named_a_node
    assert "rpc_url" in named_a_node["error"]

    assert LIST_STORES_TOOL["inputSchema"]["required"] == []


def test_a_private_rpc_url_is_refused_by_the_ssrf_guard() -> None:
    rpc = FakeRpc([])
    out = list_stores_result(
        {"network": "mainnet", "rpc_url": "http://169.254.169.254/"}, rpc_call=rpc
    )
    assert "error" in out and "rpc_url refused" in out["error"]
    assert rpc.calls == []  # nothing was fetched


def test_the_result_carries_the_menu_caveat_and_no_buyable_shortlist() -> None:
    """`wired_stores` is GONE, and its absence is the point.

    It named the one storefront `prepare_purchase` had hardcoded. Now that a purchase
    re-reads whichever store it is asked for, a shortlist would be a false claim about
    which of these can be bought — the answer is all of them, so the field that implied
    otherwise is removed rather than left saying something untrue.
    """
    out = list_stores_result(
        {"network": "mainnet", "product": "water"},
        rpc_call=FakeRpc(
            _rows(("A1", encode_store("jonasbar", products=[("Water", 100_000, 6)])))
        ),
    )
    assert out["network"] == "mainnet"
    assert "wired_stores" not in out
    assert "MENU" in out["note"] and "not an authorization" in out["note"]
    # The caveat must still say the purchase re-reads rather than trusting this list.
    assert "own account" in out["note"]


def test_the_surface_serves_it() -> None:
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    surface = OrquestraCatalogSurface(purchase_rpc_call=FakeRpc(_rows()))
    names = [tool["name"] for tool in surface.list_tools()]
    assert "list_stores" in names
    out = surface.call_tool("list_stores", {"network": "mainnet"})
    assert out["stores"] == [] and out["skipped_undecodable"] == 0


def test_the_menu_says_where_an_order_would_actually_go() -> None:
    """`PurchaseMade` carries the store's telegram channel, so it is the DELIVERY ADDRESS
    and not a label. An empty one means a purchase is paid, recorded on chain, and nobody
    is ever told to make it — which a buyer should be able to see BEFORE paying rather
    than discover afterwards by the coffee not arriving."""
    out = list_stores_result(
        {"network": "mainnet"},
        rpc_call=FakeRpc(
            _rows(("A1", encode_store("geckocoffee", telegram="geckovision")))
        ),
    )
    store = out["stores"][0]

    assert store["fulfilment"]["telegram_channel_id"] == "geckovision"
    assert store["fulfilment"]["set"] is True


def test_a_store_with_no_fulfilment_channel_is_flagged_not_hidden() -> None:
    """The honest half. We cannot verify a channel RESOLVES — a wrong-but-present value
    looks exactly like a correct one — but an EMPTY one is checkable from the chain, so
    that is the one fact we can offer and it must not be silently omitted."""
    out = list_stores_result(
        {"network": "mainnet"},
        rpc_call=FakeRpc(_rows(("A1", encode_store("nobodyshome", telegram="")))),
    )

    assert out["stores"][0]["fulfilment"]["set"] is False
    # And the tool must warn, so an agent does not have to infer it from a false flag.
    assert "NOBODY" in LIST_STORES_TOOL["description"]
    assert "not a promise it resolves" in LIST_STORES_TOOL["description"]


def test_a_filter_miss_returns_near_matches_rather_than_nothing() -> None:
    """`product="coffee"` returned ZERO stores while geckocoffee sells Espresso.

    An empty array should mean "nothing on this network", never "your word did not
    literally appear in a product name". The store's own NAME contains the word, which is
    how a person got there in the first place, so a filter that ignores it sends the agent
    away from the thing it was looking for.
    """
    rpc = FakeRpc(
        _rows(
            ("A1", encode_store("geckocoffee", products=[("Espresso", 100_000, 6)])),
            ("A2", encode_store("jonasbar", products=[("Water", 100_000, 6)])),
        )
    )
    out = list_stores_result({"product": "coffee"}, rpc_call=rpc)

    assert out["stores"], "a store whose NAME matches is still a match"
    assert out["stores"][0]["store"] == "geckocoffee"
    assert out["stores"][0]["match_kind"] == "store_name"
    # …and the unrelated store is still excluded, or the filter would mean nothing.
    assert [s["store"] for s in out["stores"]] == ["geckocoffee"]


def test_a_product_name_match_is_reported_as_the_stronger_kind() -> None:
    rpc = FakeRpc(
        _rows(("A1", encode_store("jonasbar", products=[("Water", 100_000, 6)])))
    )

    out = list_stores_result({"product": "wat"}, rpc_call=rpc)

    assert out["stores"][0]["match_kind"] == "product"


def test_a_word_matching_nothing_still_returns_nothing() -> None:
    """The widening must not become "everything matches". An empty result has to stay
    possible, or the filter stops carrying information."""
    rpc = FakeRpc(
        _rows(("A1", encode_store("jonasbar", products=[("Water", 100_000, 6)])))
    )

    assert list_stores_result({"product": "lobster"}, rpc_call=rpc)["stores"] == []


# -- the look-alike asset ----------------------------------------------------------


#: USDG on mainnet — 6 decimals, "a dollar stablecoin" to a person, and owned by
#: Token-2022. Presented beside USDC with only a label, the two are indistinguishable.
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class MintAwareRpc:
    """A node that answers the program scan AND `getMultipleAccounts` on the mints.

    ``owners`` maps mint -> owning program, or to ``None`` for "no account here". A mint
    absent from the map comes back as a null entry, which is what a node says about an
    address that is not a mint at all.
    """

    def __init__(
        self, rows: list[dict[str, Any]], owners: dict[str, str | None]
    ) -> None:
        self.rows = rows
        self.owners = owners
        self.calls: list[tuple[str, list[Any]]] = []

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "getProgramAccounts":
            return {"result": self.rows}
        if method == "getMultipleAccounts":
            values: list[dict[str, Any] | None] = []
            for mint in params[0]:
                owner = self.owners.get(mint)
                values.append(
                    None
                    if owner is None
                    else {"owner": owner, "lamports": 1, "data": ["", "base64"]}
                )
            return {"result": {"value": values}}
        raise AssertionError(f"unexpected method {method}")


def test_two_stores_priced_in_look_alike_mints_are_distinguishable_on_the_menu() -> (
    None
):
    """The hole: both products say "a dollar stablecoin"; only the token program differs.

    An agent holding classic-SPL USDC reading a Token-2022 price has, without this field,
    nothing on the surface telling it the two are not interchangeable — the ATA, the
    transfer instruction and the signing program are all different.
    """
    rpc = MintAwareRpc(
        _rows(
            ("A1", encode_store("geckocoffee", products=[("Espresso", 100_000, 6)])),
            (
                "B1",
                encode_store("usdgbar", products=[("Espresso", 100_000, 6)], mint=USDG),
            ),
        ),
        owners={USDC: CLASSIC, USDG: TOKEN_2022},
    )

    out = list_stores(rpc_url="http://node", rpc_call=rpc)
    by_store = {
        store["store"]: store["products"][0]["token_program"] for store in out["stores"]
    }

    assert by_store["geckocoffee"] == {
        "address": CLASSIC,
        "name": "classic-spl-token",
        "read": True,
        "recognised": True,
    }
    assert by_store["usdgbar"] == {
        "address": TOKEN_2022,
        "name": "token-2022",
        "read": True,
        "recognised": True,
    }
    # …and the two are told apart by the FIELD, not by the label, which is identical.
    assert by_store["geckocoffee"]["name"] != by_store["usdgbar"]["name"]


def test_an_unreadable_mint_is_unknown_and_never_assumed_classic() -> None:
    """Fail closed: the common case is classic SPL, which is exactly why the default
    cannot be classic SPL. Unknown and classic must not be the same value."""
    rpc = MintAwareRpc(_rows(("A1", encode_store("geckocoffee"))), owners={USDC: None})

    program = list_stores(rpc_url="http://node", rpc_call=rpc)["stores"][0]["products"][
        0
    ]["token_program"]

    assert program["read"] is False
    assert program["address"] is None
    assert program["name"] == "unknown" != "classic-spl-token"
    assert "no account exists" in program["reason"]


def test_a_node_that_cannot_answer_the_mint_read_still_lists_the_menu() -> None:
    """An RPC failure on the mint read degrades the FIELD, never the directory: a menu
    that vanishes because one lookup failed is worse than a menu with a stated unknown."""

    def rpc(url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getProgramAccounts":
            return {"result": _rows(("A1", encode_store("geckocoffee")))}
        raise RpcError("JSON-RPC getMultipleAccounts failed: code=-32603")

    out = list_stores(rpc_url="http://node", rpc_call=rpc)
    program = out["stores"][0]["products"][0]["token_program"]

    assert out["stores"][0]["store"] == "geckocoffee"
    assert program["read"] is False and program["name"] == "unknown"
    assert "RpcError" in program["reason"]


def test_the_token_program_is_read_from_the_mint_not_inferred() -> None:
    """The rule that matters more than the field. A mint whose owner is neither token
    program reports THAT owner — it is not coerced into a known name, and the fact that
    the value tracks the node's answer proves nothing is inferred from the address."""
    odd_owner = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
    rpc = MintAwareRpc(
        _rows(("A1", encode_store("geckocoffee"))), owners={USDC: odd_owner}
    )

    program = list_stores(rpc_url="http://node", rpc_call=rpc)["stores"][0]["products"][
        0
    ]["token_program"]

    assert program == {
        "address": odd_owner,
        "name": odd_owner,
        "read": True,
        "recognised": False,
    }
    # And the mint the label calls "USDC" is still labelled — the note is additive.
    assert (
        list_stores(rpc_url="http://node", rpc_call=rpc)["stores"][0]["products"][0][
            "mint_note"
        ]
        == "USDC"
    )


def test_each_distinct_mint_is_read_once_per_call_not_once_per_product() -> None:
    rpc = MintAwareRpc(
        _rows(
            (
                "A1",
                encode_store(
                    "geckocoffee",
                    products=[
                        ("Espresso", 100_000, 6),
                        ("Water", 50_000, 6),
                        ("Wine", 200_000, 6),
                    ],
                ),
            ),
            ("A2", encode_store("jonasbar", products=[("Water", 100_000, 6)])),
        ),
        owners={USDC: CLASSIC},
    )

    list_stores(rpc_url="http://node", rpc_call=rpc)

    mint_reads = [
        params for method, params in rpc.calls if method == "getMultipleAccounts"
    ]
    assert len(mint_reads) == 1, "one batched round trip for the whole directory"
    assert mint_reads[0][0] == [USDC], "four products, one distinct mint, one read"


def test_a_filtered_product_still_carries_its_token_program() -> None:
    """The field must survive both filter paths — a product-name match and the widening
    to a store-name match — or an agent that searched would lose the very warning it needs.
    """
    rpc = MintAwareRpc(
        _rows(("A1", encode_store("geckocoffee", products=[("Espresso", 100_000, 6)]))),
        owners={USDC: CLASSIC},
    )

    matched = list_stores(rpc_url="http://node", rpc_call=rpc, product="espresso")
    widened = list_stores(rpc_url="http://node", rpc_call=rpc, product="coffee")

    for out in (matched, widened):
        assert (
            out["stores"][0]["products"][0]["token_program"]["name"]
            == "classic-spl-token"
        )


def test_the_menu_says_a_label_is_not_an_asset() -> None:
    """The cheapest half of the fix: say it, so an agent need not infer it from a field
    it has never seen before."""
    out = list_stores_result(
        {"network": "mainnet"},
        rpc_call=MintAwareRpc(
            _rows(("A1", encode_store("geckocoffee"))), owners={USDC: CLASSIC}
        ),
    )

    assert "label" in out["mint_note_caveat"]
    assert "token-2022" in out["mint_note_caveat"]
    assert "cannot pay" in out["mint_note_caveat"].lower()
    assert "token_program" in LIST_STORES_TOOL["description"]


def test_the_surface_quotes_one_blockhash_clock() -> None:
    """The clock was ~40s in list_stores and ~60s in prepare_purchase — one event,
    two figures. One constant now; no tool text may carry its own number."""
    from gecko.prepare_purchase import BLOCKHASH_CLOCK_PROSE

    assert BLOCKHASH_CLOCK_PROSE in LIST_STORES_TOOL["description"]
    assert "~40-second" not in LIST_STORES_TOOL["description"]
