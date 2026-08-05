"""Pump.fun SELL-side curve math (gecko/pump_curve.py), offline-falsifiable.

The point of this file is that a sell is **not** a buy with the sign flipped. Three
things differ, and each one has a test that fails if someone "simplifies" the sell path
into the buy path:

  * the denominator ADDS the input (``vToken + amount``) where a buy subtracts it;
  * there is no ``+1`` lamport (that term is buy-only);
  * there is no ``real_token_reserves`` cap (a seller may push past it).

Source for all three: ``@pump-fun/pump-sdk@1.36.0`` ``src/bondingCurve.ts`` —
``getSellSolAmountFromTokenAmountQuote`` = ``inputAmount.mul(virtualQuoteReserves)
.div(virtualTokenReserves.add(inputAmount))``, wrapped by
``getSellSolAmountFromTokenAmount`` = ``solCost.sub(getFee(...))``.

Pinned against the SAME real mainnet reserves the buy tests use, so the two directions
are comparable on one curve.
"""

from __future__ import annotations

import base64
import struct

import pytest

from gecko.pump_curve import (
    BondingCurveReserves,
    CurveError,
    apply_slippage,
    apply_slippage_down,
    buy_base_sol_cost,
    decode_bonding_curve_reserves,
    quote_min_sol_output,
    sell_base_sol_output,
)

# Real mainnet reserves for EExN5XX…p3tH (the same fixture the buy tests pin against).
REAL_VTOKEN = 1_065_644_468_288_345
REAL_VSOL = 30_207_084_547
REAL_RTOKEN = 785_744_468_288_345

REAL = BondingCurveReserves(
    virtual_token_reserves=REAL_VTOKEN,
    virtual_sol_reserves=REAL_VSOL,
    real_token_reserves=REAL_RTOKEN,
    complete=False,
)


def _curve_blob(
    vtoken: int = REAL_VTOKEN,
    vsol: int = REAL_VSOL,
    rtoken: int = REAL_RTOKEN,
    *,
    complete: bool = False,
    cashback: bool = False,
    mayhem: bool = False,
    size: int = 151,
) -> str:
    """A BondingCurve blob with the reserve fields AND the two shape flags at their real
    offsets (complete@48, creator@49..81, is_mayhem_mode@81, is_cashback_coin@82)."""
    raw = bytearray(size)
    struct.pack_into("<Q", raw, 8, vtoken)
    struct.pack_into("<Q", raw, 16, vsol)
    struct.pack_into("<Q", raw, 24, rtoken)
    if complete:
        raw[48] = 1
    if mayhem:
        raw[81] = 1
    if cashback:
        raw[82] = 1
    return base64.b64encode(bytes(raw)).decode()


# --- the formula itself -----------------------------------------------------------


@pytest.mark.parametrize(
    "amount,expected",
    [
        (1_000_000, 28),  # amount * vSol // (vToken + amount), no +1
        (1_000_000_000_000, 28_319_731),
        (100_000_000_000_000, 2_591_449_225),
    ],
)
def test_sell_output_matches_the_sdk_formula(amount: int, expected: int) -> None:
    assert sell_base_sol_output(amount, REAL) == expected
    # …and it is exactly the closed form, recomputed here rather than trusted.
    assert expected == amount * REAL_VSOL // (REAL_VTOKEN + amount)


def test_sell_is_not_the_buy_formula_reversed() -> None:
    """The regression that matters: on the SAME curve and amount, a sell yields strictly
    LESS than a buy costs — the denominator moves the other way and there is no +1. If
    someone routes sell through buy_base_sol_cost, this fails."""
    amount = 1_000_000
    assert sell_base_sol_output(amount, REAL) == 28
    assert buy_base_sol_cost(amount, REAL) == 29
    assert sell_base_sol_output(amount, REAL) < buy_base_sol_cost(amount, REAL)


def test_sell_has_no_real_token_reserve_cap() -> None:
    """A buy caps the amount at real_token_reserves (you cannot take more than exist); a
    sell does not (tokens are going back IN). Selling above the cap must still price."""
    above_cap = REAL_RTOKEN + 1_000_000_000
    out = sell_base_sol_output(above_cap, REAL)
    assert out == above_cap * REAL_VSOL // (REAL_VTOKEN + above_cap)
    # a capped implementation would have priced it as if amount == real_token_reserves
    assert out != REAL_RTOKEN * REAL_VSOL // (REAL_VTOKEN + REAL_RTOKEN)


def test_sell_rejects_non_positive_amount_and_graduated_curve() -> None:
    with pytest.raises(CurveError):
        sell_base_sol_output(0, REAL)
    graduated = BondingCurveReserves(REAL_VTOKEN, REAL_VSOL, REAL_RTOKEN, complete=True)
    with pytest.raises(CurveError) as exc:
        sell_base_sol_output(1_000, graduated)
    assert "PumpSwap" in str(exc.value)


# --- the guard direction ----------------------------------------------------------


def test_slippage_shrinks_a_sell_floor_and_grows_a_buy_ceiling() -> None:
    # The sign error that silently gives away money (or trips 6003) if inverted.
    assert apply_slippage_down(1_000, 500) == 950
    assert apply_slippage(1_000, 500) == 1_051  # buy: grows, +1
    assert apply_slippage_down(1_000, 0) == 1_000


def test_slippage_down_rejects_out_of_range_bps() -> None:
    with pytest.raises(CurveError):
        apply_slippage_down(1_000, -1)
    with pytest.raises(CurveError):
        apply_slippage_down(1_000, 10_001)  # a floor cannot be negative money


def test_quote_min_sol_output_pins_both_numbers() -> None:
    base, floor = quote_min_sol_output(1_000_000_000_000, REAL, 500)
    assert (base, floor) == (28_319_731, 26_903_744)
    # the gap must absorb the protocol + creator fee the program deducts from proceeds
    assert floor < base


# --- the two flags that decide the ACCOUNT SHAPE ----------------------------------


def test_decode_reads_cashback_and_mayhem_flags() -> None:
    plain = decode_bonding_curve_reserves(_curve_blob())
    assert (plain.is_cashback_coin, plain.is_mayhem_mode) == (False, False)
    cash = decode_bonding_curve_reserves(_curve_blob(cashback=True))
    assert (cash.is_cashback_coin, cash.is_mayhem_mode) == (True, False)
    mayhem = decode_bonding_curve_reserves(_curve_blob(mayhem=True))
    assert (mayhem.is_cashback_coin, mayhem.is_mayhem_mode) == (False, True)


def test_short_blob_decodes_to_the_conservative_shape() -> None:
    """A blob written before those fields existed must not read garbage into the shape
    decision — it decodes to the 16-account (non-cashback) default."""
    short = decode_bonding_curve_reserves(_curve_blob(size=50))
    assert short.is_cashback_coin is False
    assert short.is_mayhem_mode is False
