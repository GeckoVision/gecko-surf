"""The packaged Orca Whirlpool config derives the accounts a REAL mainnet swap uses.

Every expectation here was read off mainnet (slot 441,556,950) and cross-checked
against the account list Jupiter's own router passes when it routes USDG -> USDC
through pool 9RqDTfwC... These are the two recipes the catalog surface got wrong,
so this test is the guard that keeps them right:

* ``whirlpool`` — the surface derives the 5th seed as a RESOLVER on
  ``adaptive_fee_tier.fee_tier_index`` (runtime data), which makes the pool
  underivable. It is a caller-supplied ``u16`` LE ``tick_spacing``, and a wrong
  fee tier silently derives a real, well-formed, WRONG pool.
* ``tick_array`` — the surface reads the IDL arg ``start_tick_index: i32`` and
  encodes 4-byte LE. Orca seeds with the ASCII DECIMAL STRING. The LE form yields
  well-formed, uninitialized, wrong accounts.

An arg's IDL type does not determine its seed encoding — that is the lesson these
assertions are pinning down.
"""

import pytest

from gecko.pda import derive_pda
from gecko.provider_config import load_packaged_provider

pytest.importorskip("solders", reason="PDA derivation needs the [solana] extra")

WHIRLPOOLS_CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL = "9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps"
ORACLE = "4FQqY5C4fjReyc3MkMRqbR7bk9KRzXdCTbAXb5wbycVh"


@pytest.fixture(scope="module")
def pdas():
    _, apis = load_packaged_provider("orquestra")
    program = apis["whirlpool"].program
    assert program is not None, "whirlpool config carries no program spec"
    return dict(program.pdas)


def test_whirlpool_pool_derives_from_mints_and_tick_spacing(pdas):
    got = derive_pda(
        pdas["whirlpool"],
        {
            "whirlpools_config": WHIRLPOOLS_CONFIG,
            "token_mint_a": USDG,
            "token_mint_b": USDC,
            "tick_spacing": 1,
        },
    )
    assert got.address == POOL


@pytest.mark.parametrize(
    "tick_spacing, wrong_pool",
    [
        (2, "8RosFZADnHd9TaK5a6Czy6groS7xpxUd4eDRiuaSYgaz"),
        (64, "aw99i9EpTmomdbziRiD2x2Mkv9UwyazyDfUmEKAW5Hc"),
    ],
)
def test_wrong_fee_tier_derives_a_different_real_looking_pool(
    pdas, tick_spacing, wrong_pool
):
    """tick_spacing is NOT derivable from the mint pair. This is why it must be a
    caller input: every fee tier yields a well-formed address, and only one has
    money in it. Nothing about the failure is loud."""
    got = derive_pda(
        pdas["whirlpool"],
        {
            "whirlpools_config": WHIRLPOOLS_CONFIG,
            "token_mint_a": USDG,
            "token_mint_b": USDC,
            "tick_spacing": tick_spacing,
        },
    )
    assert got.address == wrong_pool
    assert got.address != POOL


def test_mint_order_is_byte_order_not_caller_order(pdas):
    """Reversing the mints derives a pool that does not exist on chain."""
    reversed_ = derive_pda(
        pdas["whirlpool"],
        {
            "whirlpools_config": WHIRLPOOLS_CONFIG,
            "token_mint_a": USDC,
            "token_mint_b": USDG,
            "tick_spacing": 1,
        },
    )
    assert reversed_.address != POOL


def test_oracle_derives_from_the_pool(pdas):
    assert derive_pda(pdas["oracle"], {"whirlpool": POOL}).address == ORACLE


@pytest.mark.parametrize(
    "start_tick_index, expected",
    [
        ("0", "2QRj3Ug2RZ9ffSCP3pp7U6ex45adrnMW7u5HAihfH2mE"),
        ("-88", "6o9yaeyc8rHKKbdRxN8M3F9Qii5zpBC33gH2L1GUNBPj"),
        ("-176", "94cnSkfZfnpkS8yBs1XuqugRUJWruTkMhMPPPfiBdapm"),
    ],
)
def test_tick_arrays_match_the_accounts_a_real_swap_passes(
    pdas, start_tick_index, expected
):
    """The three tick arrays Jupiter's router passes for this pool at
    tick_current_index=2. Seeded with the ASCII decimal index, not LE bytes."""
    got = derive_pda(
        pdas["tick_array"],
        {"whirlpool": POOL, "start_tick_index": start_tick_index},
    )
    assert got.address == expected
