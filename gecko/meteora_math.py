"""Meteora DLMM lb_pair state read + bin/price math — the STATE half of a landable swap.

Deriving accounts is geometry; a DLMM ``swap`` that LANDS needs two things only live
state can give:

  1. **the bin_array account set** — the swap traverses price bins stored in per-70-bin
     ``BinArray`` PDAs that travel as *remaining accounts the IDL never names*. Which
     arrays are needed depends on ``lb_pair.active_id`` AND the pool's liquidity bitmap:
     the array holding the active bin may not even exist (verified live: pool
     ``EtAdVRLFH…`` has active array −2 missing while −1/0/1 exist), so "active +
     adjacent" fabricates a dead account. This module ports the SDK's own bitmap walk
     (``get_bin_array_pubkeys_for_swap`` → ``next_bin_array_index_with_liquidity_internal``,
     MeteoraAg/dlmm-sdk ``commons/src/quote.rs`` + ``extensions/lb_pair.rs``);
  2. **``min_amount_out``** — the slippage guard the IDL cannot price. The active-bin
     price is ``(1 + bin_step/10000)^active_id`` in Q64.64 (``commons/src/math/
     price_math.rs::get_price_from_id``), ported bit-faithfully below.

HONESTY — what the quote is and is not: ``min_amount_out`` is computed from a SNAPSHOT
of the active bin's price. It ignores swap fees (base + variable) and multi-bin
traversal, and the active bin moves between quote and land. The protection is the
on-chain slippage guard (the quote minus ``slippage_bps``) plus the simulation Receipt —
NOT the prediction. A too-tight slippage trips the program's own check; the default is
deliberately generous.

Layout facts (verified against the Orquestra-served IDL field list AND the real mainnet
account — the byte sum lands exactly on the 904-byte LbPair, and the stored ``oracle``
pubkey at offset 552 matches its derived PDA): ``active_id`` i32 @76, ``bin_step`` u16
@80, ``status`` u8 @82, ``token_x_mint`` @88, ``token_y_mint`` @120,
``bin_array_bitmap`` [u64;16] @584.

Control-plane invariant #1: only these fields are decoded IN MEMORY from public account
metadata; the account payload is never stored.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

from .rpc import LOCAL_RPC, RpcCall, default_rpc_call

__all__ = [
    "BASIS_POINT_MAX",
    "BIN_ARRAY_BITMAP_SIZE",
    "DEFAULT_SLIPPAGE_BPS",
    "MAX_BIN_PER_ARRAY",
    "LbPairState",
    "MeteoraMathError",
    "apply_min_out_slippage",
    "bin_id_to_bin_array_index",
    "decode_lb_pair_state",
    "price_from_id",
    "quote_min_amount_out",
    "read_lb_pair_state",
    "swap_bin_array_indexes",
    "swap_for_y",
]

# LbPair field byte offsets (8-byte Anchor discriminator + StaticParameters[32] +
# VariableParameters[32] + bump_seed[1] + bin_step_seed[2] + pair_type[1] = 76).
_ACTIVE_ID_OFF = 76
_BIN_STEP_OFF = 80
_STATUS_OFF = 82
_TOKEN_X_MINT_OFF = 88
_TOKEN_Y_MINT_OFF = 120
_BIN_ARRAY_BITMAP_OFF = 584  # [u64; 16] little-endian limbs, 1024 bits
_BIN_ARRAY_BITMAP_LIMBS = 16

# Program constants (dlmm-sdk commons/src/constants.rs; BinArray.bins is [Bin; 70] in
# the served IDL too).
MAX_BIN_PER_ARRAY = 70
BIN_ARRAY_BITMAP_SIZE = 512  # internal bitmap covers array indexes [-512, 511]
BASIS_POINT_MAX = 10_000

# Q64.64 fixed-point (dlmm-sdk commons/src/math/u64x64_math.rs).
_SCALE_OFFSET = 64
_ONE_Q64 = 1 << _SCALE_OFFSET
_U128_MAX = (1 << 128) - 1
_MAX_EXPONENTIAL = 0x80000  # exponents beyond this overflow Q64.64 (19 bits)

# Generous default: must cover swap fees (base + variable) AND multi-bin price impact
# AND active-bin movement, none of which the snapshot quote models. Callers can tighten
# it; too tight trips the on-chain ExceededAmountSlippageTolerance guard.
DEFAULT_SLIPPAGE_BPS = 500  # 5%


class MeteoraMathError(Exception):
    """An lb_pair read/decode/quote failure — a missing pool account, a truncated
    payload, a mint not in the pool, or a quote that cannot be honoured. Messages carry
    only public data (addresses, offsets, integer state) — never a secret."""


@dataclass(frozen=True)
class LbPairState:
    """The swap-relevant LbPair fields — decoded in memory from the public pool
    account, never stored. ``bin_array_bitmap`` is the 1024-bit internal liquidity
    bitmap as one int (bit ``i`` = array index ``i - 512`` has liquidity)."""

    active_id: int
    bin_step: int
    status: int
    token_x_mint: str
    token_y_mint: str
    bin_array_bitmap: int


def decode_lb_pair_state(data_b64: str) -> LbPairState:
    """Decode the swap-relevant fields from a base64 ``LbPair`` account blob.

    Only the fields at the verified offsets are read; the rest of the payload is
    ignored and never retained. Raises :class:`MeteoraMathError` if the blob is too
    short to hold them.
    """
    raw = base64.b64decode(data_b64)
    need = _BIN_ARRAY_BITMAP_OFF + 8 * _BIN_ARRAY_BITMAP_LIMBS
    if len(raw) < need:
        raise MeteoraMathError(
            f"lb_pair data is {len(raw)} bytes — too short to decode swap state "
            f"(need {need})"
        )
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover - needs the [solana] extra
        raise MeteoraMathError(
            "decoding lb_pair state needs the 'solana' extra: install with "
            "`pip install gecko-surf[solana]` (or `uv add solders`)"
        ) from exc

    active_id = struct.unpack_from("<i", raw, _ACTIVE_ID_OFF)[0]
    bin_step = struct.unpack_from("<H", raw, _BIN_STEP_OFF)[0]
    status = raw[_STATUS_OFF]
    token_x = str(Pubkey.from_bytes(raw[_TOKEN_X_MINT_OFF : _TOKEN_X_MINT_OFF + 32]))
    token_y = str(Pubkey.from_bytes(raw[_TOKEN_Y_MINT_OFF : _TOKEN_Y_MINT_OFF + 32]))
    limbs = struct.unpack_from(
        f"<{_BIN_ARRAY_BITMAP_LIMBS}Q", raw, _BIN_ARRAY_BITMAP_OFF
    )
    bitmap = 0
    for i, limb in enumerate(limbs):
        bitmap |= limb << (64 * i)
    return LbPairState(
        active_id=active_id,
        bin_step=bin_step,
        status=status,
        token_x_mint=token_x,
        token_y_mint=token_y,
        bin_array_bitmap=bitmap,
    )


def read_lb_pair_state(
    lb_pair: str,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> LbPairState:
    """``getAccountInfo`` the pool and decode its swap state — a control-plane read of
    public metadata (never stored). The RPC is injectable so this is unit-testable
    offline. Raises :class:`MeteoraMathError` if the pool is absent or dataless.
    """
    call = rpc_call or default_rpc_call
    resp = call(rpc_url, "getAccountInfo", [lb_pair, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise MeteoraMathError(
            f"lb_pair {lb_pair} not found on-chain — cannot read swap state"
        )
    data = value.get("data")
    if not (isinstance(data, list) and data and isinstance(data[0], str)):
        raise MeteoraMathError(
            f"lb_pair {lb_pair} has no base64 data to decode swap state from"
        )
    return decode_lb_pair_state(data[0])


def swap_for_y(state: LbPairState, input_mint: str) -> bool:
    """True iff the swap sells token X for token Y (``input_mint`` is the pool's X).

    Direction decides which way the bin walk goes AND which side of the price the
    quote uses. Raises :class:`MeteoraMathError` if the mint is not in the pool —
    a wrong-pool plan must fail loudly, not price nonsense.
    """
    if input_mint == state.token_x_mint:
        return True
    if input_mint == state.token_y_mint:
        return False
    raise MeteoraMathError(
        f"input mint {input_mint} is neither token_x ({state.token_x_mint}) nor "
        f"token_y ({state.token_y_mint}) of this pool"
    )


def bin_id_to_bin_array_index(bin_id: int) -> int:
    """The index of the 70-bin ``BinArray`` holding ``bin_id`` — floor division.

    Python's ``//`` IS floor division, which matches the SDK's ``div_rem`` +
    subtract-1-when-negative-remainder exactly (bin_array.rs
    ``bin_id_to_bin_array_index``): −78 // 70 → −2.
    """
    return bin_id // MAX_BIN_PER_ARRAY


def _next_index_with_liquidity(
    bitmap: int, start_index: int, walk_down: bool
) -> tuple[int, bool]:
    """Port of ``next_bin_array_index_with_liquidity_internal`` (lb_pair.rs).

    From ``start_index`` (inclusive), find the nearest array index with liquidity in
    the walk direction within the internal 1024-bit bitmap. Returns ``(index, True)``
    on a hit; ``(out_of_range_index, False)`` when the rest of the direction is empty
    (the SDK then consults the bitmap-*extension* account — see the caller's honesty
    note).
    """
    offset = start_index + BIN_ARRAY_BITMAP_SIZE  # bit position, 0..1023
    if walk_down:
        # keep bits [0, offset]: the SDK shifts left by (1023 - offset) in U1024;
        # highest remaining set bit is the nearest index at-or-below start.
        masked = bitmap & ((1 << (offset + 1)) - 1)
        if masked == 0:
            return (-BIN_ARRAY_BITMAP_SIZE - 1, False)
        return (masked.bit_length() - 1 - BIN_ARRAY_BITMAP_SIZE, True)
    masked = bitmap >> offset
    if masked == 0:
        return (BIN_ARRAY_BITMAP_SIZE, False)
    trailing = (masked & -masked).bit_length() - 1
    return (start_index + trailing, True)


def swap_bin_array_indexes(
    state: LbPairState, input_mint: str, count: int = 3
) -> list[int]:
    """The bin_array indexes a swap of ``input_mint`` needs, nearest-first.

    Port of the SDK's ``get_bin_array_pubkeys_for_swap`` over the pool's INTERNAL
    liquidity bitmap: start at the active bin's array, walk in the swap direction
    (down for X→Y, up for Y→X), and take up to ``count`` arrays that actually hold
    liquidity — skipping gaps instead of fabricating dead accounts (the active array
    itself may not exist).

    HONEST LIMIT: liquidity beyond array index ±512 lives behind the separate
    ``bin_array_bitmap_extension`` account, which this walk does not read; the list is
    simply truncated there (extension-range pools are exotic — bin ids beyond ±35840).
    Raises :class:`MeteoraMathError` if NO array in the direction holds liquidity —
    an unswappable (drained) pool must fail loudly.
    """
    walk_down = swap_for_y(state, input_mint)
    start = bin_id_to_bin_array_index(state.active_id)
    increment = -1 if walk_down else 1
    indexes: list[int] = []
    while len(indexes) < count:
        if start < -BIN_ARRAY_BITMAP_SIZE or start >= BIN_ARRAY_BITMAP_SIZE:
            break  # internal bitmap exhausted — extension territory, stop honestly
        index, has_liquidity = _next_index_with_liquidity(
            state.bin_array_bitmap, start, walk_down
        )
        if not has_liquidity:
            break
        indexes.append(index)
        start = index + increment
    if not indexes:
        raise MeteoraMathError(
            f"no bin array holds liquidity {'below' if walk_down else 'above'} the "
            f"active bin (active_id={state.active_id}) — pool is not swappable in "
            "this direction"
        )
    return indexes


def _pow_q64(base: int, exp: int) -> int:
    """Bit-faithful port of the SDK's Q64.64 ``pow`` (u64x64_math.rs).

    Square-and-multiply over the 19 exponent bits, inverting via ``U128_MAX / x``
    exactly as the source does (so results match the on-chain math bit-for-bit,
    including its rounding).
    """
    invert = exp < 0
    if exp == 0:
        return _ONE_Q64
    exponent = abs(exp)
    if exponent >= _MAX_EXPONENTIAL:
        raise MeteoraMathError(f"bin exponent {exp} overflows Q64.64 price math")
    squared_base = base
    result = _ONE_Q64
    if squared_base >= result:
        squared_base = _U128_MAX // squared_base
        invert = not invert
    for bit in range(19):
        if exponent & (1 << bit):
            result = (result * squared_base) >> _SCALE_OFFSET
        squared_base = (squared_base * squared_base) >> _SCALE_OFFSET
    if result == 0:
        raise MeteoraMathError(f"bin exponent {exp} underflows Q64.64 price math")
    if invert:
        result = _U128_MAX // result
    return result


def price_from_id(active_id: int, bin_step: int) -> int:
    """The bin's price (token Y per token X, raw units) in Q64.64.

    ``(1 + bin_step/10000)^active_id`` — port of ``get_price_from_id``
    (price_math.rs). Negative ids price below 1.0; the Q64.64 int is exact, no
    floats anywhere.
    """
    bps = (bin_step << _SCALE_OFFSET) // BASIS_POINT_MAX
    return _pow_q64(_ONE_Q64 + bps, active_id)


def apply_min_out_slippage(expected_out: int, slippage_bps: int) -> int:
    """Shrink an expected output by ``slippage_bps`` → the ``min_amount_out`` guard.

    The guard must absorb the fees and price movement the snapshot quote does not
    model; too tight trips the on-chain slippage check. Raises on bps outside
    [0, 10000).
    """
    if not 0 <= slippage_bps < BASIS_POINT_MAX:
        raise MeteoraMathError(
            f"slippage_bps must be in [0, {BASIS_POINT_MAX}), got {slippage_bps}"
        )
    return expected_out * (BASIS_POINT_MAX - slippage_bps) // BASIS_POINT_MAX


def quote_min_amount_out(
    amount_in: int,
    state: LbPairState,
    input_mint: str,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
) -> tuple[int, int]:
    """``(expected_out_snapshot, min_amount_out)`` for swapping ``amount_in``.

    The expected output prices the WHOLE amount at the active bin (SDK
    ``Bin::get_amount_out``, floor-rounded both directions): X→Y is
    ``amount_in × price >> 64``; Y→X is ``amount_in << 64 // price``. SNAPSHOT
    semantics — see the module docstring; the slippage-padded guard is the real
    protection. Raises on a non-positive amount or a zero-output quote (dust in,
    nothing out — the guard would be meaningless).
    """
    if amount_in <= 0:
        raise MeteoraMathError(f"amount_in must be positive, got {amount_in}")
    price = price_from_id(state.active_id, state.bin_step)
    if swap_for_y(state, input_mint):
        expected = (amount_in * price) >> _SCALE_OFFSET
    else:
        expected = (amount_in << _SCALE_OFFSET) // price
    min_out = apply_min_out_slippage(expected, slippage_bps)
    if min_out <= 0:
        raise MeteoraMathError(
            f"quote for amount_in={amount_in} rounds to zero output at the active "
            "bin price — amount too small to guard with min_amount_out"
        )
    return expected, min_out
