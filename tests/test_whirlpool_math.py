"""The output floor that shipped as ZERO on mainnet five times, pinned so it cannot again.

`other_amount_threshold: 0` on an exact-input swap means ACCEPT ANY OUTPUT. Only
`sqrt_price_limit` protected those transactions. These tests exist so that the value is
never again a literal, and so the sizing half and the floor half can never disagree.
"""

import pytest

from gecko.meteora_math import MeteoraMathError
from gecko.whirlpool_math import (
    FEE_RATE_DENOMINATOR,
    WhirlpoolMathError,
    quote_min_amount_out,
    size_input_for_output,
    spot_out,
)

#: 1.0 exactly in Q64.64 — a 1:1 pool, the USDG/USDC shape the mainnet runs used.
ONE = 1 << 64
#: Orca states fee_rate in hundredths of a bip; the live USDG/USDC pool is 100 == 0.01%.
FEE = 100


def test_a_zero_floor_is_refused_not_returned() -> None:
    """The whole point. An amount too small to guard yields a floor of 0, and a floor of 0
    is the ABSENCE of a floor — so it is refused rather than handed back as a number."""
    with pytest.raises(WhirlpoolMathError, match="accept any output"):
        quote_min_amount_out(1, ONE // 10**9, FEE, a_to_b=True, slippage_bps=100)


def test_the_floor_is_below_spot_and_above_zero() -> None:
    spot, floor = quote_min_amount_out(101_001, ONE, FEE, a_to_b=True, slippage_bps=100)
    assert spot == 101_001  # 1:1 pool, so out == in before costs
    assert 0 < floor < spot
    assert floor == 99_980  # less 0.01% fee, less 100 bps


def test_sizing_and_floor_are_exact_inverses() -> None:
    """The bug this pair replaces: the sizing printed 101,001 against a 100,000 price while
    the swap's own bounds needed 101,022. It cleared on a good fill, which is luck, not a
    guarantee. Sizing must clear the floor BY CONSTRUCTION."""
    price = 100_000
    need = size_input_for_output(price, ONE, FEE, a_to_b=True, slippage_bps=100)
    assert need == 101_022
    _spot, floor = quote_min_amount_out(need, ONE, FEE, a_to_b=True, slippage_bps=100)
    assert floor >= price, (
        "the sized input must clear the floor it will be judged against"
    )


@pytest.mark.parametrize("price", [1, 999, 100_000, 12_345_678, 5 * 10**9])
@pytest.mark.parametrize("a_to_b", [True, False])
@pytest.mark.parametrize("sqrt_price", [ONE, ONE * 3 // 2, ONE // 4, ONE * 40])
def test_the_inverse_holds_across_prices_and_directions(
    price: int, a_to_b: bool, sqrt_price: int
) -> None:
    """Not just at 1:1 with equal decimals. That coincidence is what hid the unit error."""
    need = size_input_for_output(
        price, sqrt_price, FEE, a_to_b=a_to_b, slippage_bps=100
    )
    _spot, floor = quote_min_amount_out(
        need, sqrt_price, FEE, a_to_b=a_to_b, slippage_bps=100
    )
    assert floor >= price


def test_direction_actually_changes_the_answer() -> None:
    """A positive control: if a_to_b were ignored, the test above would pass vacuously."""
    lopsided = ONE * 7 // 2
    assert spot_out(1_000_000, lopsided, a_to_b=True) != spot_out(
        1_000_000, lopsided, a_to_b=False
    )


def test_a_bps_above_the_denominator_is_refused_not_wrapped() -> None:
    """An inline `(10_000 - bps) // 10_000` goes NEGATIVE here — a floor of 'accept
    anything' wearing a number. The shared guard raises instead."""
    with pytest.raises(MeteoraMathError):
        quote_min_amount_out(100_000, ONE, FEE, a_to_b=True, slippage_bps=10_000)
    with pytest.raises(WhirlpoolMathError):
        size_input_for_output(100_000, ONE, FEE, a_to_b=True, slippage_bps=10_001)


def test_nonsense_pool_state_is_refused() -> None:
    for kwargs in (
        {"sqrt_price": 0, "fee_rate": FEE},
        {"sqrt_price": ONE, "fee_rate": FEE_RATE_DENOMINATOR},
        {"sqrt_price": ONE, "fee_rate": -1},
    ):
        with pytest.raises(WhirlpoolMathError):
            quote_min_amount_out(100_000, a_to_b=True, slippage_bps=100, **kwargs)


def test_zero_and_negative_amounts_are_refused() -> None:
    for amount in (0, -1):
        with pytest.raises(WhirlpoolMathError):
            quote_min_amount_out(amount, ONE, FEE, a_to_b=True, slippage_bps=100)
        with pytest.raises(WhirlpoolMathError):
            size_input_for_output(amount, ONE, FEE, a_to_b=True, slippage_bps=100)


def test_the_script_no_longer_carries_a_literal_zero_threshold() -> None:
    """Guards the actual regression: the value reached mainnet as a literal in the script,
    not through any function these tests could reach."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "scripts" / "prepare_whirlpool_swap.py"
    ).read_text()
    assert '"other_amount_threshold": 0' not in src
    assert '"other_amount_threshold": min_out' in src
