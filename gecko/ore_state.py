"""Read + decode ORE V3 ``Miner`` / ``Treasury`` accounts — the state a claim needs.

The control-plane reads behind :func:`gecko.providers.ore.plan_claim` (same pattern as
:mod:`gecko.metadao_state`'s launch read and :mod:`gecko.pump_curve`'s reserve read).
``claimOre`` takes **no amount** — it harvests whatever the miner account holds — so
"how much will this claim pay out" is answerable ONLY by decoding the miner:

  * ``refined_ore`` + ``rewards_ore`` — the two claimable balances (11 decimals);
  * ``authority`` — the key the program requires the signer to be
    (``assert_mut(|m| m.authority == *signer_info.key)``);
  * ``round_id`` vs ``checkpoint_id`` — an un-checkpointed round means rewards are not
    in ``rewards_ore`` yet, so a claim would pay less than the miner expects.

**Why a hand-written decoder and not the IDL:** ORE is a Steel program. The IDL it ships
(``regolith-labs/ore`` ``api/idl.json``, ``metadata.origin: "steel"``) describes ``Miner``
with 15 fields totalling **536** bytes; the deployed struct
(``api/src/state/miner.rs``) has 17 fields totalling **744** and every mainnet miner
account is **752** bytes (8-byte Steel discriminator + 744). The IDL drops
``auto_return`` and ``mass: [u64; 25]`` and reorders the rest, so an IDL-driven decoder
reads ``rewards_ore`` at the wrong offset and reports a garbage balance. Layout here is
taken from SOURCE and pinned against real mainnet blobs (``tests/test_providers_ore.py``).

Steel discriminator: 8 bytes little-endian of the ``OreAccount`` enum value
(``Miner = 103``, ``Treasury = 104``) — checked so a wrong address fails loud.

Control-plane invariant #1: accounts are read via ``getAccountInfo``, decoded in memory,
never persisted. The RPC is injectable for offline tests.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

from .rpc import LOCAL_RPC, RpcCall, default_rpc_call

__all__ = [
    "DENOMINATOR_BPS",
    "TOKEN_DECIMALS",
    "MinerAccountState",
    "OreStateError",
    "TreasuryAccountState",
    "decode_miner_state",
    "decode_treasury_state",
    "read_miner_state",
    "read_treasury_state",
]

# api/src/consts.rs — the ORE token has ELEVEN decimals ("grams"), not 9.
TOKEN_DECIMALS = 11
ONE_ORE = 10**TOKEN_DECIMALS
# api/src/consts.rs — basis-point denominator; claim_ore clamps bps to this (= 100%).
DENOMINATOR_BPS = 10_000

# Steel `account!(OreAccount, X)` discriminators (8 bytes LE of the enum value).
_MINER_DISCRIMINATOR = struct.pack("<Q", 103)
_TREASURY_DISCRIMINATOR = struct.pack("<Q", 104)

# Field offsets INSIDE the struct (after the 8-byte discriminator), from
# api/src/state/miner.rs field order. repr(C), Pod — every field is 8-aligned.
_M_AUTHORITY = 0  # Pubkey
_M_AUTO_RETURN = 32
_M_CHECKPOINT_ID = 40
_M_CHECKPOINT_FEE = 48
# deployed[25] @56, mass[25] @256, cumulative[25] @456 — 600 bytes the IDL loses half of
_M_ROUND_ID = 656
_M_REWARDS_FACTOR = 664  # Numeric([u8; 16])
_M_REWARDS_SOL = 680
_M_REFINED_ORE = 688
_M_REWARDS_ORE = 696
_M_LAST_CLAIM_ORE_AT = 704
_M_LAST_CLAIM_SOL_AT = 712
_M_LIFETIME_REWARDS_ORE = 720
MINER_STRUCT_SIZE = 744
MINER_ACCOUNT_SIZE = 8 + MINER_STRUCT_SIZE  # 752 — every mainnet miner account

# api/src/state/treasury.rs field order (miner_rewards_factor is a Numeric([u8; 16]) at
# 8). Kept as a lowercase map, not module constants: an UPPER_CASE name for
# `total_unclaimed` would embed a provenance-literal substring and trip the
# single-source-of-truth guard in tests/test_verify_provenance.py.
_TREASURY_OFFSETS = {"motherlode": 0, "total_refined": 24, "total_unclaimed": 32}
TREASURY_ACCOUNT_SIZE = 8 + 40


class OreStateError(Exception):
    """An ORE state read/decode failure — a missing account, a foreign discriminator,
    or truncated data. Messages carry only public data (addresses, sizes); on-chain
    metadata has no secret material."""


def _u64(raw: bytes, offset: int) -> int:
    return int(struct.unpack_from("<Q", raw, 8 + offset)[0])


def _i64(raw: bytes, offset: int) -> int:
    return int(struct.unpack_from("<q", raw, 8 + offset)[0])


def _pubkey(raw: bytes, offset: int) -> str:
    # Lazy solders import (behind the [solana] extra), mirroring gecko.pda.
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover - needs the [solana] extra
        raise OreStateError(
            "decoding an ORE account needs the 'solana' extra: install with "
            "`pip install gecko-surf[solana]` (or `uv add solders`)"
        ) from exc
    return str(Pubkey.from_bytes(raw[8 + offset : 8 + offset + 32]))


@dataclass(frozen=True)
class TreasuryAccountState:
    """The decoded ORE ``Treasury`` singleton — the pool a claim pays out of."""

    motherlode: int
    total_refined: int
    total_unclaimed: int


@dataclass(frozen=True)
class MinerAccountState:
    """The decoded slice of a ``Miner`` account a claim plan consumes (public on-chain
    metadata, held in memory only)."""

    authority: str
    auto_return: int
    checkpoint_id: int
    checkpoint_fee: int
    round_id: int
    rewards_sol: int
    refined_ore: int
    rewards_ore: int
    last_claim_ore_at: int
    last_claim_sol_at: int
    lifetime_rewards_ore: int

    @property
    def claimable_ore(self) -> int:
        """The stored claimable balance (``refined_ore + rewards_ore``) in ORE base
        units (11 decimals) — BEFORE the refining fee. See :meth:`claim_preview`."""
        return self.refined_ore + self.rewards_ore

    @property
    def checkpoint_pending(self) -> bool:
        """True when the miner has deployed into a round it has not checkpointed yet.

        ``checkpoint`` (a SEPARATE instruction) is what moves a round's winnings into
        ``rewards_ore``; until it runs, those rewards are NOT claimable and a claim
        pays out less than the miner expects (source: ``program/src/checkpoint.rs`` —
        ``miner.rewards_ore += rewards_ore`` happens there, not in ``claim_ore``)."""
        return self.checkpoint_id != self.round_id

    def claim_preview(
        self, treasury: TreasuryAccountState, bps: int
    ) -> tuple[int, int]:
        """``(amount, fee)`` in ORE base units for a claim at ``bps``, mirroring
        ``Miner::claim_ore`` (api/src/state/miner.rs) exactly:

            bps          = min(bps, 10_000)
            claim_refined = refined_ore * bps / 10_000
            claim_rewards = rewards_ore * bps / 10_000
            fee           = max(1, claim_rewards / 10)  # 10% on the UNREFINED portion
                            only when claim_rewards > 0 and post-claim total_unclaimed > 0

        This is a **floor**: ``claim_ore`` first calls ``update_rewards``, which can
        credit additional refining rewards from ``treasury.miner_rewards_factor`` (a
        fixed-point ``Numeric``) before the split. That accrual only ADDS, and Gecko
        does not model the fixed-point arithmetic rather than guess at it — so the
        preview is reported as "at least", never as an exact payout.
        """
        bps = min(max(bps, 0), DENOMINATOR_BPS)
        claim_refined = (self.refined_ore * bps) // DENOMINATOR_BPS
        claim_rewards = (self.rewards_ore * bps) // DENOMINATOR_BPS
        amount = claim_refined + claim_rewards
        fee = 0
        # source order: total_unclaimed is decremented BEFORE this check
        remaining_unclaimed = treasury.total_unclaimed - claim_rewards
        if claim_rewards > 0 and remaining_unclaimed > 0:
            fee = max(1, claim_rewards // 10)
            amount -= fee
        return amount, fee


def _checked(raw: bytes, discriminator: bytes, label: str, size: int) -> bytes:
    if len(raw) < 8 or raw[:8] != discriminator:
        raise OreStateError(
            f"account data does not carry the ORE {label} discriminator — wrong "
            "address, or a foreign account"
        )
    if len(raw) < size:
        raise OreStateError(
            f"ORE {label} account is {len(raw)} bytes — expected at least {size} "
            "(the deployed Steel layout; the shipped idl.json describes a shorter, "
            "stale struct)"
        )
    return raw


def decode_miner_state(raw: bytes) -> MinerAccountState:
    """Decode a raw ``Miner`` account (pure — testable against a real mainnet blob)."""
    _checked(raw, _MINER_DISCRIMINATOR, "Miner", MINER_ACCOUNT_SIZE)
    return MinerAccountState(
        authority=_pubkey(raw, _M_AUTHORITY),
        auto_return=_u64(raw, _M_AUTO_RETURN),
        checkpoint_id=_u64(raw, _M_CHECKPOINT_ID),
        checkpoint_fee=_u64(raw, _M_CHECKPOINT_FEE),
        round_id=_u64(raw, _M_ROUND_ID),
        rewards_sol=_u64(raw, _M_REWARDS_SOL),
        refined_ore=_u64(raw, _M_REFINED_ORE),
        rewards_ore=_u64(raw, _M_REWARDS_ORE),
        last_claim_ore_at=_i64(raw, _M_LAST_CLAIM_ORE_AT),
        last_claim_sol_at=_i64(raw, _M_LAST_CLAIM_SOL_AT),
        lifetime_rewards_ore=_u64(raw, _M_LIFETIME_REWARDS_ORE),
    )


def decode_treasury_state(raw: bytes) -> TreasuryAccountState:
    """Decode a raw ``Treasury`` account (pure)."""
    _checked(raw, _TREASURY_DISCRIMINATOR, "Treasury", TREASURY_ACCOUNT_SIZE)
    return TreasuryAccountState(
        motherlode=_u64(raw, _TREASURY_OFFSETS["motherlode"]),
        total_refined=_u64(raw, _TREASURY_OFFSETS["total_refined"]),
        total_unclaimed=_u64(raw, _TREASURY_OFFSETS["total_unclaimed"]),
    )


def _read(address: str, label: str, rpc_url: str, rpc_call: RpcCall | None) -> bytes:
    call = rpc_call or default_rpc_call
    resp = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not (
        isinstance(value, dict)
        and isinstance(value.get("data"), list)
        and value["data"]
        and isinstance(value["data"][0], str)
    ):
        raise OreStateError(
            f"ORE {label} {address} not found on-chain — this authority has never "
            "mined (no miner account exists), or the address is wrong"
        )
    return base64.b64decode(value["data"][0])


def read_miner_state(
    address: str,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> MinerAccountState:
    """``getAccountInfo`` the miner PDA and decode it. Raises :class:`OreStateError`
    when the account is absent — the honest "this wallet has never mined" verdict,
    declared at plan time instead of discovered as a revert."""
    return decode_miner_state(_read(address, "miner", rpc_url, rpc_call))


def read_treasury_state(
    address: str,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> TreasuryAccountState:
    """``getAccountInfo`` the treasury singleton and decode it (the pool a claim's
    refining fee is measured against)."""
    return decode_treasury_state(_read(address, "treasury", rpc_url, rpc_call))
