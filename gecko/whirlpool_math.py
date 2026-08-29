"""Orca Whirlpool price math — the OUTPUT FLOOR a landable swap needs, and its inverse.

Deriving the accounts for ``swap_v2`` is geometry, and the packaged recipes already do it.
What geometry cannot supply is the answer to *what will you not accept* — and this module
exists because we shipped that answer as ``0`` five times on mainnet.

``other_amount_threshold: 0`` on an exact-input swap means ACCEPT ANY OUTPUT. Only
``sqrt_price_limit`` stood between the caller and an arbitrarily bad fill. gecko's own
``prepare_instruction`` states the rule that violated:

    a minimum-amount or slippage argument is how you say what you will NOT accept, and
    omitting it is refused rather than defaulted

``gecko/meteora_math.py`` already got this right for DLMM. This is the Whirlpool half, and
it deliberately reuses that module's ``apply_min_out_slippage`` so the bps range is
validated in exactly one place — an inline ``(10_000 - bps) // 10_000`` goes NEGATIVE above
10000 bps, which is a floor of "accept anything" wearing a number.

THE PRICE. ``Whirlpool.sqrt_price`` is Q64.64, so ``(sqrt_price / 2**64) ** 2`` is base units
of token B per base unit of token A. Squaring keeps it exact in Q128.128 — every operation
here is integer, no floats anywhere.

HONESTY — what this quote is and is not. It prices the whole amount at the pool's CURRENT
spot price. It ignores price impact and multi-tick traversal, both of which make the real
output SMALLER. So a derived floor is OPTIMISTIC, and that is the safe direction: the
program reverts rather than filling below it. It is a guard, not a quote. Callers who want
an exact floor state one instead of deriving it.
"""

from __future__ import annotations

from gecko.meteora_math import apply_min_out_slippage

__all__ = [
    "WhirlpoolMathError",
    "FEE_RATE_DENOMINATOR",
    "spot_out",
    "quote_min_amount_out",
    "size_input_for_output",
]

#: Orca states fee_rate in hundredths of a basis point: 100 == 0.01%.
FEE_RATE_DENOMINATOR = 1_000_000

_Q128 = 1 << 128


class WhirlpoolMathError(ValueError):
    """A quote that cannot be guarded is refused rather than returned."""


def _ceil(numerator: int, denominator: int) -> int:
    """Integer ceiling. Sizing rounds UP at every step: rounding down produces an input
    that lands one base unit short of the floor it was sized to clear."""
    return -(-numerator // denominator)


def _check(sqrt_price: int, fee_rate: int) -> None:
    if sqrt_price <= 0:
        raise WhirlpoolMathError(f"sqrt_price must be positive, got {sqrt_price}")
    if not 0 <= fee_rate < FEE_RATE_DENOMINATOR:
        raise WhirlpoolMathError(
            f"fee_rate must be in [0, {FEE_RATE_DENOMINATOR}), got {fee_rate}"
        )


def spot_out(amount_in: int, sqrt_price: int, *, a_to_b: bool) -> int:
    """Output at the pool's current price, before fee and before slippage.

    ``a_to_b`` spends token A and receives B; the reverse receives A. Floor-rounded in both
    directions, matching how the program itself truncates.
    """
    _check(sqrt_price, 0)
    if amount_in <= 0:
        raise WhirlpoolMathError(f"amount_in must be positive, got {amount_in}")
    sq = sqrt_price * sqrt_price
    return (amount_in * sq) >> 128 if a_to_b else (amount_in << 128) // sq


def _floor_for(
    amount_in: int, sqrt_price: int, fee_rate: int, *, a_to_b: bool, slippage_bps: int
) -> int:
    """The floor a given input would be guarded by. Non-raising, so the search below can
    probe freely; the public entry points are the ones that refuse."""
    expected = spot_out(amount_in, sqrt_price, a_to_b=a_to_b)
    after_fee = expected * (FEE_RATE_DENOMINATOR - fee_rate) // FEE_RATE_DENOMINATOR
    return apply_min_out_slippage(after_fee, slippage_bps)


def quote_min_amount_out(
    amount_in: int,
    sqrt_price: int,
    fee_rate: int,
    *,
    a_to_b: bool,
    slippage_bps: int,
) -> tuple[int, int]:
    """``(expected_out_at_spot, min_amount_out)`` — the value for ``other_amount_threshold``.

    Refuses when the floor rounds to zero: an amount too small to guard is an amount whose
    guard would be the ``0`` this module exists to remove.
    """
    _check(sqrt_price, fee_rate)
    expected = spot_out(amount_in, sqrt_price, a_to_b=a_to_b)
    min_out = _floor_for(
        amount_in, sqrt_price, fee_rate, a_to_b=a_to_b, slippage_bps=slippage_bps
    )
    if min_out <= 0:
        raise WhirlpoolMathError(
            f"amount_in={amount_in} rounds to a zero output floor at this price — too small "
            "to guard. A floor of 0 means 'accept any output', which is what this refuses."
        )
    return expected, min_out


def size_input_for_output(
    target_out: int,
    sqrt_price: int,
    fee_rate: int,
    *,
    a_to_b: bool,
    slippage_bps: int,
) -> int:
    """The SMALLEST input whose floor is provably at least ``target_out``.

    The inverse of :func:`quote_min_amount_out`, and the reason it exists: a caller who
    knows a PRICE (in the mint a store settles in) needs a quantity of the mint they
    actually HOLD. Those are only interchangeable when both mints share decimals AND trade
    at 1:1 — true for USDG/USDC and for nothing else, which is exactly the coincidence that
    made a wrong script look correct on mainnet.

    IT SEARCHES RATHER THAN SOLVES, and that is deliberate. The closed-form inverse is short
    by one to two base units off 1:1, because the forward direction floors three separate
    times (spot, fee, slippage) and no amount of ceiling on the way in compensates for
    rounding that happens on the way out. So the analytic value is used only as a starting
    point, and the answer returned is one this module has CHECKED against the very function
    that will judge it. A guard derived from an unverified estimate is how the 21-unit
    shortfall got onto mainnet in the first place.
    """
    _check(sqrt_price, fee_rate)
    if target_out <= 0:
        raise WhirlpoolMathError(f"target_out must be positive, got {target_out}")
    if not 0 <= slippage_bps < 10_000:
        raise WhirlpoolMathError(
            f"slippage_bps must be in [0, 10000), got {slippage_bps}"
        )

    sq = sqrt_price * sqrt_price
    base = _ceil(target_out << 128, sq) if a_to_b else _ceil(target_out * sq, _Q128)
    after_fee = _ceil(base * FEE_RATE_DENOMINATOR, FEE_RATE_DENOMINATOR - fee_rate)
    lo = max(1, _ceil(after_fee * 10_000, 10_000 - slippage_bps))

    def clears(amount: int) -> bool:
        return (
            _floor_for(
                amount, sqrt_price, fee_rate, a_to_b=a_to_b, slippage_bps=slippage_bps
            )
            >= target_out
        )

    if clears(lo):
        # The estimate already suffices; walk DOWN to the minimum so the caller is never
        # told to spend more than the guard requires.
        low, high = 1, lo
        while low < high:
            mid = (low + high) // 2
            if clears(mid):
                high = mid
            else:
                low = mid + 1
        return low

    # Short, as it is off 1:1. Double until it clears, then bisect for the least input.
    high = lo
    for _ in range(128):
        high *= 2
        if clears(high):
            break
    else:
        raise WhirlpoolMathError(
            f"no input within 2**128 of the estimate clears target_out={target_out} at "
            f"sqrt_price={sqrt_price} — the pool price is degenerate for this target"
        )
    low = lo
    while low < high:
        mid = (low + high) // 2
        if clears(mid):
            high = mid
        else:
            low = mid + 1
    return low
