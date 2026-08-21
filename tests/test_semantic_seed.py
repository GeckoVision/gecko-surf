"""encode_store round-trip + the catalogue→store-bytes seeder, offline.

The load-bearing property: encode_store is a FAITHFUL inverse of decode_store,
with the REAL Anchor discriminator — so a seeded store both reads back and is
accepted by the program on a purchase. The live fork write is exercised by
scripts/semantic_seed_fork.py (a run, not a test).
"""

from __future__ import annotations

from gecko.semantic_catalogue import CATALOGUE
from gecko.semantic_seed import USDC_DECIMALS, USDC_MINT, config_to_listing
from gecko.semantic_scenarios import SCENARIO_2_BUDGET
from gecko.store_directory import (
    StoreListing,
    StoreProduct,
    decode_store,
    encode_store,
    receipts_discriminator,
)

AUTHORITY = "DMjTEZJuV3mpfzBNeeuFy9m47A1bj5CXVhCNVo7BEPzy"
ADDR = "HVkbYf9PBF49WVViFf7eM1VescsgRHNeu4XJv1XveC8x"


def test_discriminator_matches_live_geckocoffee() -> None:
    # Read from the live account 2026-08-21: def5ed403b311df6.
    assert receipts_discriminator().hex() == "def5ed403b311df6"


def test_encode_decode_round_trip() -> None:
    listing = StoreListing(
        store_name="geckocoffee",
        address=ADDR,
        authority=AUTHORITY,
        total_purchases=17,
        products=(
            StoreProduct(
                name="Sparkling water", price_raw=50000, decimals=6, mint=USDC_MINT
            ),
            StoreProduct(name="Espresso", price_raw=100000, decimals=6, mint=USDC_MINT),
        ),
        telegram_channel_id="@geckocoffeeshop",
    )
    decoded = decode_store(encode_store(listing), address=ADDR)
    assert decoded.store_name == "geckocoffee"
    assert decoded.authority == AUTHORITY
    assert decoded.total_purchases == 17
    assert decoded.telegram_channel_id == "@geckocoffeeshop"
    assert decoded.products == listing.products


def test_full_catalogue_encodes_and_decodes() -> None:
    from gecko.semantic_catalogue import to_store_config

    config = to_store_config("geckocoffee")
    listing = config_to_listing(config, authority=AUTHORITY, address=ADDR)
    decoded = decode_store(encode_store(listing), address=ADDR)
    assert len(decoded.products) == len(CATALOGUE) == 31
    # Every catalogue price survives as the on-chain price_raw, one number not two.
    by_name = {p.name: p for p in decoded.products}
    for item in config["items"]:
        assert by_name[item["name"]].price_raw == item["price_lamports"]
        assert by_name[item["name"]].decimals == USDC_DECIMALS
        assert by_name[item["name"]].mint == USDC_MINT


def test_scenario_2_budget_still_holds_against_seeded_prices() -> None:
    # The oat-vs-budget conflict must survive seeding: 3 oat cappuccino + brewed
    # over budget, dairy under. The seeded price_raw IS the catalogue price, so
    # this is the same arithmetic the scenario tests assert — proven end to end.
    from gecko.semantic_catalogue import to_store_config

    config = to_store_config("geckocoffee")
    price = {item["name"]: item["price_lamports"] for item in config["items"]}
    oat = 3 * price["Oat Cappuccino"] + price["Brewed Coffee (drip)"]
    dairy = 3 * price["Cappuccino"] + price["Brewed Coffee (drip)"]
    assert oat > SCENARIO_2_BUDGET >= dairy


def test_read_store_on_fork_falls_back_to_program_accounts() -> None:
    # surfpool quirk: getAccountInfo returns null on a just-seeded store while
    # getProgramAccounts already carries it. read_store_on_fork must find it.
    import base64

    from gecko.semantic_seed import config_to_listing, read_store_on_fork
    from gecko.store_directory import LET_ME_BUY_PROGRAM_ID, encode_store

    from gecko.semantic_catalogue import to_store_config

    listing = config_to_listing(
        to_store_config("geckocoffee"), authority=AUTHORITY, address=ADDR
    )
    encoded = base64.b64encode(encode_store(listing)).decode()

    def rpc(url: str, method: str, params: list) -> dict:
        if method == "getAccountInfo":
            return {"value": None}  # the lag
        if method == "getProgramAccounts":
            return {
                "result": [{"pubkey": ADDR, "account": {"data": [encoded, "base64"]}}]
            }
        raise AssertionError(method)

    found = read_store_on_fork(
        ADDR, "http://fork", rpc, program_id=LET_ME_BUY_PROGRAM_ID
    )
    assert found is not None and len(found.products) == 31


def test_read_store_on_fork_returns_none_when_absent() -> None:
    from gecko.semantic_seed import read_store_on_fork

    def rpc(url: str, method: str, params: list) -> dict:
        return {"value": None} if method == "getAccountInfo" else {"result": []}

    assert read_store_on_fork(ADDR, "http://fork", rpc) is None


def test_config_to_listing_carries_authority_and_telegram() -> None:
    from gecko.semantic_catalogue import to_store_config

    listing = config_to_listing(
        to_store_config("geckocoffee"),
        authority=AUTHORITY,
        address=ADDR,
        telegram_channel_id="@test",
    )
    assert listing.authority == AUTHORITY
    assert listing.telegram_channel_id == "@test"
    assert listing.total_purchases == 0  # a freshly seeded store has sold nothing
