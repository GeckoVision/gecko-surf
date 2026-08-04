"""Pump.fun bonding-curve read + buy-price math (gecko/pump_curve.py), offline-falsifiable.

The curve math is pinned two ways: a constructed fixture with hand-verified arithmetic,
and the REAL mainnet bonding_curve reserves (verified via RPC in this session). No network:
the reserve read is exercised with an injected rpc_call.
"""

from __future__ import annotations

import base64
import struct
from typing import Any

import pytest

from gecko.pump_curve import (
    BondingCurveReserves,
    CurveError,
    apply_slippage,
    buy_base_sol_cost,
    decode_bonding_curve_reserves,
    quote_max_sol_cost,
    read_bonding_curve_reserves,
)

# Real mainnet reserves for EExN5XX…p3tH (RPC-verified: virtual_token @8, virtual_sol @16,
# real_token @24, complete @48). Buying 1_000_000 base units off this curve costs 29
# lamports pre-fee; +5% slippage → max_sol_cost 31 (both hand-computed from the formula).
REAL_VTOKEN = 1_065_644_468_288_345
REAL_VSOL = 30_207_084_547
REAL_RTOKEN = 785_744_468_288_345
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"


def _curve_blob(vtoken: int, vsol: int, rtoken: int, complete: bool = False) -> str:
    """A 151-byte BondingCurve blob with the reserve fields at the real offsets."""
    raw = bytearray(151)
    struct.pack_into("<Q", raw, 8, vtoken)
    struct.pack_into("<Q", raw, 16, vsol)
    struct.pack_into("<Q", raw, 24, rtoken)
    raw[48] = 1 if complete else 0
    return base64.b64encode(bytes(raw)).decode()


# --- decode ------------------------------------------------------------------


def test_decode_reads_reserves_at_the_right_offsets() -> None:
    blob = _curve_blob(REAL_VTOKEN, REAL_VSOL, REAL_RTOKEN)
    got = decode_bonding_curve_reserves(blob)
    assert got.virtual_token_reserves == REAL_VTOKEN
    assert got.virtual_sol_reserves == REAL_VSOL
    assert got.real_token_reserves == REAL_RTOKEN
    assert got.complete is False


def test_decode_reads_complete_flag() -> None:
    assert decode_bonding_curve_reserves(_curve_blob(1, 1, 1, complete=True)).complete


def test_decode_too_short_raises() -> None:
    with pytest.raises(CurveError):
        decode_bonding_curve_reserves(base64.b64encode(b"\x00" * 16).decode())


# --- buy price math (constant product) --------------------------------------


def test_buy_base_sol_cost_hand_verified() -> None:
    # capped=500_000, denom=1_000_000-500_000=500_000, cost=500_000*500_000//500_000+1
    reserves = BondingCurveReserves(1_000_000, 500_000, 1_000_000, False)
    assert buy_base_sol_cost(500_000, reserves) == 500_001


def test_buy_base_sol_cost_real_reserves() -> None:
    reserves = BondingCurveReserves(REAL_VTOKEN, REAL_VSOL, REAL_RTOKEN, False)
    assert buy_base_sol_cost(1_000_000, reserves) == 29


def test_apply_slippage_pads_and_ceils() -> None:
    # 500_001 * 11000 // 10000 + 1 = 550_001 + 1 = 550_002 (10% + the +1 guard)
    assert apply_slippage(500_001, 1000) == 550_002


def test_quote_max_sol_cost_real_reserves_default_slippage() -> None:
    reserves = BondingCurveReserves(REAL_VTOKEN, REAL_VSOL, REAL_RTOKEN, False)
    base, max_cost = quote_max_sol_cost(1_000_000, reserves)  # default 500 bps
    assert base == 29
    # 29 * 10500 // 10000 + 1 = 30 + 1 = 31
    assert max_cost == 31


def test_buy_on_complete_curve_raises() -> None:
    reserves = BondingCurveReserves(REAL_VTOKEN, REAL_VSOL, REAL_RTOKEN, complete=True)
    with pytest.raises(CurveError):
        buy_base_sol_cost(1_000_000, reserves)


def test_buy_non_positive_amount_raises() -> None:
    reserves = BondingCurveReserves(1_000_000, 500_000, 1_000_000, False)
    with pytest.raises(CurveError):
        buy_base_sol_cost(0, reserves)


def test_apply_negative_slippage_raises() -> None:
    with pytest.raises(CurveError):
        apply_slippage(100, -1)


# --- read (injected rpc, no network) ----------------------------------------


def test_read_bonding_curve_reserves_via_injected_rpc() -> None:
    blob = _curve_blob(REAL_VTOKEN, REAL_VSOL, REAL_RTOKEN)

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        assert method == "getAccountInfo" and params[0] == BONDING_CURVE
        return {"result": {"value": {"owner": "x", "data": [blob, "base64"]}}}

    got = read_bonding_curve_reserves(BONDING_CURVE, rpc_url="http://x", rpc_call=rpc)
    assert got.virtual_sol_reserves == REAL_VSOL


def test_read_missing_account_raises() -> None:
    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"result": {"value": None}}

    with pytest.raises(CurveError):
        read_bonding_curve_reserves(BONDING_CURVE, rpc_url="http://x", rpc_call=rpc)
