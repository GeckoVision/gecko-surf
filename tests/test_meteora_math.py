"""The DLMM state read + bin/price math (gecko/meteora_math.py), offline-falsifiable.

Fixtures are canned LbPair blobs built at the layout offsets VERIFIED two ways: the
Orquestra-served IDL field list byte-sums exactly to the real 904-byte account, and the
stored oracle pubkey @552 matches its derived PDA. The pinned field values (active_id
−2607, bin_step 10) are the real mainnet SOL/USDC pool
``BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y`` read on 2026-08-04; the price pin is
cross-checked against an independent high-precision computation of
``(1 + bin_step/10000)^active_id`` (rel. err < 1e-16).
"""

from __future__ import annotations

import base64
import struct
from typing import Any

import pytest

from gecko.meteora_math import (
    LbPairState,
    MeteoraMathError,
    apply_min_out_slippage,
    bin_id_to_bin_array_index,
    decode_lb_pair_state,
    price_from_id,
    quote_min_amount_out,
    read_lb_pair_state,
    swap_bin_array_indexes,
    swap_for_y,
)

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER = "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump"
LB_PAIR = "BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y"

# Real mainnet facts (2026-08-04): the SOL/USDC pool's swap state.
ACTIVE_ID = -2607
BIN_STEP = 10
# (1 + 10/10000)^-2607 in Q64.64 — the port's exact output, cross-checked against
# Decimal to <1e-16 relative error (≈ 0.0738516… token Y per token X, raw units).
PRICE_Q64 = 1_362_321_886_622_002_812


def _pubkey_bytes(addr: str) -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(addr))


def _lb_pair_blob(
    active_id: int = ACTIVE_ID,
    bin_step: int = BIN_STEP,
    liquidity_indexes: tuple[int, ...] = tuple(range(-54, -29)),
) -> str:
    """A 904-byte LbPair blob with the swap fields at the VERIFIED offsets: active_id
    i32 @76, bin_step u16 @80, mints @88/@120, bin_array_bitmap [u64;16] @584."""
    raw = bytearray(904)
    struct.pack_into("<i", raw, 76, active_id)
    struct.pack_into("<H", raw, 80, bin_step)
    raw[88:120] = _pubkey_bytes(WSOL)  # token_x
    raw[120:152] = _pubkey_bytes(USDC)  # token_y
    bitmap = 0
    for index in liquidity_indexes:
        bitmap |= 1 << (index + 512)
    for limb in range(16):
        struct.pack_into(
            "<Q", raw, 584 + 8 * limb, (bitmap >> (64 * limb)) & ((1 << 64) - 1)
        )
    return base64.b64encode(bytes(raw)).decode()


def _state(**overrides: Any) -> LbPairState:
    return decode_lb_pair_state(_lb_pair_blob(**overrides))


# --- decode / read ------------------------------------------------------------


def test_decode_lb_pair_state_reads_verified_offsets() -> None:
    state = _state()
    assert state.active_id == ACTIVE_ID
    assert state.bin_step == BIN_STEP
    assert state.token_x_mint == WSOL
    assert state.token_y_mint == USDC
    assert state.bin_array_bitmap >> (512 - 54) & 1  # index -54 flagged


def test_decode_negative_active_id_is_signed() -> None:
    # active_id is i32 — the real pool's is negative; an unsigned decode would give
    # a wildly positive id (and a wrong bin array set).
    assert _state(active_id=-78).active_id == -78


def test_decode_too_short_blob_raises() -> None:
    with pytest.raises(MeteoraMathError):
        decode_lb_pair_state(base64.b64encode(b"\0" * 100).decode())


def test_read_lb_pair_state_via_injected_rpc() -> None:
    blob = _lb_pair_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        assert method == "getAccountInfo" and params[0] == LB_PAIR
        return {"result": {"value": {"owner": "x", "data": [blob, "base64"]}}}

    state = read_lb_pair_state(LB_PAIR, rpc_url="http://127.0.0.1:8899", rpc_call=rpc)
    assert state.active_id == ACTIVE_ID


def test_read_missing_pool_raises() -> None:
    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"result": {"value": None}}

    with pytest.raises(MeteoraMathError):
        read_lb_pair_state(LB_PAIR, rpc_url="http://127.0.0.1:8899", rpc_call=rpc)


# --- direction / bin array index ---------------------------------------------


def test_swap_for_y_direction_and_wrong_mint() -> None:
    state = _state()
    assert swap_for_y(state, WSOL) is True  # selling X
    assert swap_for_y(state, USDC) is False  # selling Y
    with pytest.raises(MeteoraMathError):
        swap_for_y(state, OTHER)  # not in the pool — fail loudly, never price


def test_bin_id_to_bin_array_index_is_floor_division() -> None:
    # matches the SDK's div_rem + subtract-1-on-negative-remainder exactly
    assert bin_id_to_bin_array_index(0) == 0
    assert bin_id_to_bin_array_index(69) == 0
    assert bin_id_to_bin_array_index(70) == 1
    assert bin_id_to_bin_array_index(-1) == -1
    assert bin_id_to_bin_array_index(-70) == -1
    assert bin_id_to_bin_array_index(-71) == -2
    assert bin_id_to_bin_array_index(-78) == -2  # the real EtAd… pool's active bin
    assert bin_id_to_bin_array_index(-2607) == -38  # the real SOL/USDC pool's


# --- the bitmap walk (the Class-1 selection) ----------------------------------


def test_swap_bin_array_indexes_walks_in_swap_direction() -> None:
    state = _state()  # liquidity -54..-30, active array -38
    assert swap_bin_array_indexes(state, WSOL) == [-38, -39, -40]  # X→Y walks down
    assert swap_bin_array_indexes(state, USDC) == [-38, -37, -36]  # Y→X walks up


def test_swap_bin_array_indexes_skips_missing_active_array() -> None:
    # The live-verified trap: pool EtAdVRLFH… has active array −2 MISSING while
    # −1/0/1 exist — "active + adjacent" would fabricate a dead account. The bitmap
    # walk skips the gap (up) and refuses honestly when the direction is drained (down).
    state = _state(active_id=-78, liquidity_indexes=(-1, 0, 1))
    assert swap_bin_array_indexes(state, USDC) == [-1, 0, 1]
    with pytest.raises(MeteoraMathError):
        swap_bin_array_indexes(state, WSOL)


def test_swap_bin_array_indexes_truncates_at_liquidity_edge() -> None:
    state = _state(liquidity_indexes=(-38, -37))
    assert swap_bin_array_indexes(state, USDC) == [
        -38,
        -37,
    ]  # fewer than count — honest


def test_swap_bin_array_indexes_drained_pool_raises() -> None:
    # The OTHER live-verified state: the demo pump-token pool's bitmap is ALL ZEROS
    # (liquidity fully pulled) — an unswappable pool must refuse, not emit accounts.
    with pytest.raises(MeteoraMathError):
        swap_bin_array_indexes(_state(liquidity_indexes=()), WSOL)


# --- price / quote ------------------------------------------------------------


def test_price_from_id_identity_and_pinned_real_bin() -> None:
    assert price_from_id(0, BIN_STEP) == 1 << 64  # bin 0 prices at exactly 1.0
    assert price_from_id(ACTIVE_ID, BIN_STEP) == PRICE_Q64


def test_price_from_id_negative_id_prices_below_one() -> None:
    assert price_from_id(-1, 10) < (1 << 64) < price_from_id(1, 10)


def test_quote_min_amount_out_both_directions_pinned() -> None:
    state = _state()
    # X→Y: 1_000_000 lamports × 0.0738516… → 73_851 micro-USDC; −5% guard → 70_158
    assert quote_min_amount_out(1_000_000, state, WSOL) == (73_851, 70_158)
    # Y→X: 1_000_000 micro-USDC ÷ price → 13_540_664 lamports; −5% → 12_863_630
    assert quote_min_amount_out(1_000_000, state, USDC) == (13_540_664, 12_863_630)


def test_quote_rejects_dust_and_bad_amounts() -> None:
    state = _state()
    with pytest.raises(MeteoraMathError):
        quote_min_amount_out(0, state, WSOL)
    with pytest.raises(MeteoraMathError):
        quote_min_amount_out(1, state, WSOL)  # rounds to zero output — no guard


def test_apply_min_out_slippage_bounds() -> None:
    assert apply_min_out_slippage(10_000, 500) == 9_500
    assert apply_min_out_slippage(10_000, 0) == 10_000
    with pytest.raises(MeteoraMathError):
        apply_min_out_slippage(10_000, 10_000)
    with pytest.raises(MeteoraMathError):
        apply_min_out_slippage(10_000, -1)
