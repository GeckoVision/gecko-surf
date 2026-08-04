"""Pump.fun bonding-curve read + buy-price math — the STATE half of a landable buy.

Deriving accounts is geometry; a buy that LANDS needs one live-state argument the IDL
cannot give: ``max_sol_cost``. A derive-only plan leaves it blind, so the caller either
guesses (→ ``TooMuchSolRequired`` slippage revert) or over-pays. This module decodes the
SAME ``bonding_curve`` account ``plan_buy`` already reads (for ``creator_vault``) and
computes the constant-product SOL cost to buy ``amount`` tokens, then applies a caller
slippage → ``max_sol_cost``.

The formula is Pump's own bonding-curve buy price (constant product ``x * y = k``):

    sol_cost = min(amount, real_token_reserves) * virtual_sol_reserves
               // (virtual_token_reserves - min(amount, real_token_reserves)) + 1

Source: the pump-fun bonding-curve math (``getBuySolAmountFromTokenAmount`` — "how much
SOL to buy X tokens", the inverse of the SDK's ``getBuyPrice``); documented at
`nirholas/pump-fun-sdk docs/bonding-curve-math.md` and matching the on-chain program's
``BondingCurve`` handler. The **+1 lamport** is part of the on-chain formula; the protocol
fee is added on TOP by the program, so the caller's slippage bps MUST comfortably cover it
(~1%). This is deliberately **not** a Jupiter quote — Jupiter does not route a
pre-graduation pump token against its bonding curve at all (see the flow gap-map).

BondingCurve account layout (8-byte Anchor discriminator + fields), verified against the
real mainnet account: ``virtual_token_reserves`` u64 @ off 8, ``virtual_sol_reserves`` u64
@ off 16, ``real_token_reserves`` u64 @ off 24, ``complete`` bool @ off 48 (``creator``
pubkey @ off 49 is what ``pda_resolve`` already reads).

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
    "buy_base_sol_cost",
    "decode_bonding_curve_reserves",
    "quote_max_sol_cost",
    "read_bonding_curve_reserves",
]

# BondingCurve field byte offsets (after the 8-byte Anchor discriminator).
_VIRTUAL_TOKEN_OFF = 8
_VIRTUAL_SOL_OFF = 16
_REAL_TOKEN_OFF = 24
_COMPLETE_OFF = 48

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
    """The three reserve fields (+ ``complete``) needed to price a buy — decoded in
    memory from the public ``bonding_curve`` account, never stored."""

    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    complete: bool


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
