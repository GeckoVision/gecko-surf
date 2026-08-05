"""Pump.fun bonding-curve read + buy/sell price math — the STATE half of a landable trade.

Deriving accounts is geometry; a trade that LANDS needs one live-state argument the IDL
cannot give: ``max_sol_cost`` on a buy, ``min_sol_output`` on a sell. A derive-only plan
leaves it blind, so the caller either guesses (→ a slippage revert) or gives value away.
This module decodes the SAME ``bonding_curve`` account ``plan_buy``/``plan_sell`` already
read (for ``creator_vault``) and prices both directions off it.

**Buy** (constant product ``x * y = k``, tokens leave the curve):

    sol_cost = min(amount, real_token_reserves) * virtual_sol_reserves
               // (virtual_token_reserves - min(amount, real_token_reserves)) + 1

Source: the pump-fun bonding-curve math (``getBuySolAmountFromTokenAmount`` — "how much
SOL to buy X tokens", the inverse of the SDK's ``getBuyPrice``); documented at
`nirholas/pump-fun-sdk docs/bonding-curve-math.md` and matching the on-chain program's
``BondingCurve`` handler. The **+1 lamport** is part of the on-chain formula; the protocol
fee is added on TOP by the program, so the caller's slippage bps MUST comfortably cover it
(~1%).

**Sell** is NOT the buy formula with the sign flipped — the denominator ADDS the input,
there is no ``+1``, there is no ``real_token_reserves`` cap, and the fee is *subtracted*
from the proceeds instead of added on top:

    sol_output = amount * virtual_sol_reserves // (virtual_token_reserves + amount)

Source: ``@pump-fun/pump-sdk@1.36.0``, ``src/bondingCurve.ts`` —
``getSellSolAmountFromTokenAmountQuote`` (``inputAmount.mul(virtualQuoteReserves)
.div(virtualTokenReserves.add(inputAmount))``), wrapped by
``getSellSolAmountFromTokenAmount`` which returns ``solCost.sub(getFee(...))``. ``getFee``
is ``ceilDiv(amount * protocol_fee_bps, 1e4)`` plus the creator fee when the curve has a
creator — tiered off ``fee_config`` by market cap, i.e. NOT derivable from the reserves
alone. Gecko therefore quotes the **pre-fee** proceeds and requires the caller's slippage
bps to cover the fee, exactly as the shipped buy path does (a floor that is too HIGH trips
``TooLittleSolReceived`` 6003).

This is deliberately **not** a Jupiter quote — Jupiter does not route a pre-graduation
pump token against its bonding curve at all (see the flow gap-map).

BondingCurve account layout (8-byte Anchor discriminator + fields), verified against real
mainnet accounts: ``virtual_token_reserves`` u64 @ off 8, ``virtual_sol_reserves``
(``virtual_quote_reserves`` in current IDL naming) u64 @ off 16, ``real_token_reserves``
u64 @ off 24, ``complete`` bool @ off 48 (``creator`` pubkey @ off 49 is what
``pda_resolve`` already reads), ``is_mayhem_mode`` bool @ off 81, ``is_cashback_coin``
bool @ off 82. The last two decide the SELL account SHAPE — see
:mod:`gecko.providers.pumpfun`.

Control-plane invariant #1: only these reserve fields are decoded IN MEMORY from public
account metadata; the account payload is never stored.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

from .rpc import LOCAL_RPC, RpcCall, default_rpc_call

__all__ = [
    "DEFAULT_SLIPPAGE_BPS",
    "BondingCurveReserves",
    "CurveError",
    "apply_slippage",
    "apply_slippage_down",
    "buy_base_sol_cost",
    "decode_bonding_curve_reserves",
    "quote_max_sol_cost",
    "quote_min_sol_output",
    "read_bonding_curve_reserves",
    "sell_base_sol_output",
]

# BondingCurve field byte offsets (after the 8-byte Anchor discriminator).
_VIRTUAL_TOKEN_OFF = 8
_VIRTUAL_SOL_OFF = 16
_REAL_TOKEN_OFF = 24
_COMPLETE_OFF = 48
# creator pubkey occupies 49..81 (pda_resolve reads it); the two flags follow.
_IS_MAYHEM_OFF = 81
_IS_CASHBACK_OFF = 82

# A generous default that comfortably covers the ~1% protocol fee the program adds on top
# of the constant-product cost plus normal price movement between quote and land. The
# caller can tighten it; too-tight trips the on-chain slippage guard (TooMuchSolRequired).
DEFAULT_SLIPPAGE_BPS = 500  # 5%


class CurveError(Exception):
    """A bonding-curve read/decode/quote failure — a missing curve account, a truncated
    payload, a non-positive amount, or a graduated (``complete``) curve. Messages carry
    only public data (addresses, offsets, integer reserves) — never a secret."""


@dataclass(frozen=True)
class BondingCurveReserves:
    """The three reserve fields (+ ``complete``) needed to price a trade, plus the two
    flags that decide a SELL's account shape — decoded in memory from the public
    ``bonding_curve`` account, never stored.

    ``is_cashback_coin`` and ``is_mayhem_mode`` default to ``False`` so a blob written
    before those fields existed (or truncated) decodes to the conservative shape.
    """

    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    complete: bool
    is_cashback_coin: bool = False
    is_mayhem_mode: bool = False


def decode_bonding_curve_reserves(data_b64: str) -> BondingCurveReserves:
    """Decode the reserve fields from a base64 ``bonding_curve`` account blob.

    Only the u64s at the known offsets (+ the ``complete`` flag) are read; the rest of the
    payload is ignored and never retained. Raises :class:`CurveError` if the blob is too
    short to hold the fields.
    """
    raw = base64.b64decode(data_b64)
    if len(raw) < _REAL_TOKEN_OFF + 8:
        raise CurveError(
            f"bonding_curve data is {len(raw)} bytes — too short to decode reserves"
        )
    virtual_token = struct.unpack_from("<Q", raw, _VIRTUAL_TOKEN_OFF)[0]
    virtual_sol = struct.unpack_from("<Q", raw, _VIRTUAL_SOL_OFF)[0]
    real_token = struct.unpack_from("<Q", raw, _REAL_TOKEN_OFF)[0]
    complete = bool(raw[_COMPLETE_OFF]) if len(raw) > _COMPLETE_OFF else False
    return BondingCurveReserves(
        virtual_token_reserves=virtual_token,
        virtual_sol_reserves=virtual_sol,
        real_token_reserves=real_token,
        complete=complete,
        is_cashback_coin=bool(raw[_IS_CASHBACK_OFF])
        if len(raw) > _IS_CASHBACK_OFF
        else False,
        is_mayhem_mode=bool(raw[_IS_MAYHEM_OFF])
        if len(raw) > _IS_MAYHEM_OFF
        else False,
    )


def read_bonding_curve_reserves(
    bonding_curve: str,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> BondingCurveReserves:
    """``getAccountInfo`` the ``bonding_curve`` PDA and decode its reserves — a
    control-plane read of public metadata (never stored). The RPC is injectable so this is
    unit-testable offline. Raises :class:`CurveError` if the account is absent or dataless.
    """
    call = rpc_call or default_rpc_call
    resp = call(rpc_url, "getAccountInfo", [bonding_curve, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise CurveError(
            f"bonding_curve {bonding_curve} not found on-chain — cannot quote max_sol_cost"
        )
    data = value.get("data")
    if not (isinstance(data, list) and data and isinstance(data[0], str)):
        raise CurveError(
            f"bonding_curve {bonding_curve} has no base64 data to decode reserves from"
        )
    return decode_bonding_curve_reserves(data[0])


def buy_base_sol_cost(amount: int, reserves: BondingCurveReserves) -> int:
    """The pre-fee SOL cost (lamports) to buy ``amount`` tokens off the curve.

    Constant product: removing ``amount`` tokens (capped at ``real_token_reserves``) from
    the virtual token reserve raises the implied SOL reserve; the cost is that rise, with
    the on-chain ``+1`` lamport. The program adds its protocol fee ON TOP — cover it with
    slippage (:func:`apply_slippage`). Raises :class:`CurveError` on a non-positive amount,
    a graduated curve, or an amount that would drain the reserve.
    """
    if amount <= 0:
        raise CurveError(f"amount must be positive, got {amount}")
    if reserves.complete:
        raise CurveError(
            "bonding curve is complete (token graduated) — a buy routes to PumpSwap, "
            "not the curve; do not price it off these reserves"
        )
    capped = min(amount, reserves.real_token_reserves)
    denominator = reserves.virtual_token_reserves - capped
    if denominator <= 0:
        raise CurveError(
            f"amount {amount} meets/exceeds virtual token reserves "
            f"{reserves.virtual_token_reserves} — cannot price"
        )
    return capped * reserves.virtual_sol_reserves // denominator + 1


def apply_slippage(base_cost: int, slippage_bps: int) -> int:
    """Grow a base cost by ``slippage_bps`` basis points → a ``max_sol_cost`` guard.

    The guard must cover the protocol fee the program adds on top of ``base_cost`` plus any
    price movement; a too-tight guard trips the on-chain slippage check. Raises
    :class:`CurveError` on a negative bps.
    """
    if slippage_bps < 0:
        raise CurveError(f"slippage_bps must be non-negative, got {slippage_bps}")
    return base_cost * (10_000 + slippage_bps) // 10_000 + 1


def quote_max_sol_cost(
    amount: int,
    reserves: BondingCurveReserves,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> tuple[int, int]:
    """``(base_sol_cost, max_sol_cost)`` for buying ``amount`` tokens — the pre-fee curve
    cost and the slippage-padded guard the ``buy`` instruction takes as ``max_sol_cost``."""
    base = buy_base_sol_cost(amount, reserves)
    return base, apply_slippage(base, slippage_bps)


def sell_base_sol_output(amount: int, reserves: BondingCurveReserves) -> int:
    """The pre-fee SOL proceeds (lamports) of selling ``amount`` tokens into the curve.

    Constant product with the input ADDED to the token reserve — see the module docstring
    for the source citation. Three differences from :func:`buy_base_sol_cost` that a
    "just invert the buy" implementation would get wrong:

    * the denominator is ``virtual_token_reserves + amount`` (a buy subtracts);
    * there is no ``+1`` lamport (that term is buy-only);
    * there is no ``real_token_reserves`` cap — a seller may push tokens back past it.

    The program then SUBTRACTS the protocol (+ creator) fee from this number, so the
    caller's ``min_sol_output`` floor must sit below it by more than the fee
    (:func:`apply_slippage_down`). Raises :class:`CurveError` on a non-positive amount, a
    graduated curve, or empty token reserves.
    """
    if amount <= 0:
        raise CurveError(f"amount must be positive, got {amount}")
    if reserves.complete:
        raise CurveError(
            "bonding curve is complete (token graduated) — a sell routes to PumpSwap, "
            "not the curve; do not price it off these reserves"
        )
    denominator = reserves.virtual_token_reserves + amount
    if denominator <= 0 or reserves.virtual_token_reserves <= 0:
        raise CurveError(
            f"virtual token reserves {reserves.virtual_token_reserves} cannot price a sell"
        )
    return amount * reserves.virtual_sol_reserves // denominator


def apply_slippage_down(base_output: int, slippage_bps: int) -> int:
    """Shrink a base output by ``slippage_bps`` basis points → a ``min_sol_output`` floor.

    The mirror of :func:`apply_slippage`: a BUY guard grows (pay at most X), a SELL guard
    shrinks (receive at least X). The gap must absorb the protocol + creator fee the
    program deducts from the proceeds plus any price movement between quote and land; too
    TIGHT a floor trips ``TooLittleSolReceived`` (6003). Raises :class:`CurveError` on a
    bps outside ``0..10000`` (a floor cannot be negative money).
    """
    if slippage_bps < 0:
        raise CurveError(f"slippage_bps must be non-negative, got {slippage_bps}")
    if slippage_bps > 10_000:
        raise CurveError(
            f"slippage_bps must be <= 10000 for a sell floor, got {slippage_bps}"
        )
    return base_output * (10_000 - slippage_bps) // 10_000


def quote_min_sol_output(
    amount: int,
    reserves: BondingCurveReserves,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> tuple[int, int]:
    """``(base_sol_output, min_sol_output)`` for selling ``amount`` tokens — the pre-fee
    curve proceeds and the slippage-discounted floor the ``sell`` instruction takes as
    ``min_sol_output``."""
    base = sell_base_sol_output(amount, reserves)
    return base, apply_slippage_down(base, slippage_bps)
