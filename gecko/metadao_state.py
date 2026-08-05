"""Read + decode a MetaDAO launchpad_v7 ``Launch`` account — the state a fund needs.

The ONE control-plane read behind :func:`gecko.providers.metadao.plan_fund` (the same
pattern as :mod:`gecko.pump_curve`'s reserve read and :mod:`gecko.meteora_math`'s pool
read). The ``Launch`` account carries facts no IDL-driven client can skip:

  * ``quote_mint`` / ``launch_quote_vault`` — the vault is NOT derivable client-side;
    the program's ``has_one = launch_quote_vault`` constraint makes the field inside
    the launch account the single source of truth, so Gecko READS it (a dotted-path
    read, the same class as pump's ``bonding_curve.creator``);
  * ``state`` + the fund window (``unix_timestamp_started + seconds_for_launch``) —
    ``fund`` requires ``LaunchState::Live`` inside the window (v07 source:
    ``instructions/fund.rs::validate``), so a plan against a closed launch is declared
    un-fundable instead of discovered at revert time.

Layout facts come from the deployed v07_launchpad source
(``metaDAOproject/programs`` ``programs/v07_launchpad/src/state/launch.rs``), Borsh
field order — the ``Vec<Pubkey>`` member list before the fields we need makes every
offset DYNAMIC, which is exactly why a hand-guessed memcmp offset fails here. Verified
against real mainnet launches (decoded quote mint == USDC, vault matches ``has_one``).

Control-plane invariant #1: the account is read via ``getAccountInfo``, decoded in
memory, never persisted. The RPC is injectable for offline tests.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

from .rpc import LOCAL_RPC, RpcCall, default_rpc_call

__all__ = [
    "LAUNCH_STATE_NAMES",
    "LaunchAccountState",
    "MetadaoStateError",
    "decode_launch_state",
    "read_launch_state",
]

# Anchor account discriminator: sha256("account:Launch")[:8] — checked so a wrong
# address (or the OldLaunch layout) fails loud instead of decoding garbage.
_LAUNCH_DISCRIMINATOR = bytes.fromhex("903333a3ce55d526")

# Borsh enum LaunchState, unit-variant order from v07 source (state/launch.rs).
LAUNCH_STATE_NAMES = ("Initialized", "Live", "Closed", "Complete", "Refunding")


class MetadaoStateError(Exception):
    """A launch-state read/decode failure — a missing account, a foreign
    discriminator, or truncated data. Messages carry only public data (addresses,
    offsets) — on-chain metadata has no secret material."""


@dataclass(frozen=True)
class LaunchAccountState:
    """The decoded slice of a ``Launch`` account a fund plan consumes (public
    on-chain metadata, held in memory only)."""

    minimum_raise_amount: int
    launch_authority: str
    launch_signer: str
    launch_quote_vault: str
    launch_base_vault: str
    base_mint: str
    quote_mint: str
    unix_timestamp_started: int | None
    unix_timestamp_closed: int | None
    total_committed_amount: int
    state_index: int
    seq_num: int
    seconds_for_launch: int

    @property
    def state_name(self) -> str:
        if 0 <= self.state_index < len(LAUNCH_STATE_NAMES):
            return LAUNCH_STATE_NAMES[self.state_index]
        return f"unknown({self.state_index})"

    def fund_window(self, now: int) -> tuple[bool, str]:
        """Whether ``fund`` would clear the program's own validate() gates at ``now``
        — state must be ``Live`` and the window (started + seconds_for_launch) open.
        Returns ``(open, reason)``; the reason strings mirror the source checks."""
        if self.state_name != "Live":
            return False, (
                f"launch state is {self.state_name} — fund requires Live "
                "(LaunchpadError::InvalidLaunchState)"
            )
        if self.unix_timestamp_started is None:
            return False, "launch carries no start timestamp — not started"
        deadline = self.unix_timestamp_started + self.seconds_for_launch
        if now >= deadline:
            return False, (
                f"fund window closed at unix {deadline} (LaunchpadError::LaunchExpired)"
            )
        return True, f"Live; fund window open until unix {deadline}"


def _pubkey(raw: bytes, offset: int) -> str:
    if len(raw) < offset + 32:
        # solders panics (not raises) on a short slice — guard before it.
        raise MetadaoStateError(
            f"Launch account data is {len(raw)} bytes — truncated mid-layout "
            f"(needed a 32-byte pubkey at offset {offset})"
        )
    # Lazy solders import (behind the [solana] extra), mirroring gecko.pda.
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover - needs the [solana] extra
        raise MetadaoStateError(
            "decoding a Launch account needs the 'solana' extra: install with "
            "`pip install gecko-surf[solana]` (or `uv add solders`)"
        ) from exc
    return str(Pubkey.from_bytes(raw[offset : offset + 32]))


def decode_launch_state(raw: bytes) -> LaunchAccountState:
    """Decode a raw ``Launch`` account (pure — testable against a real blob).

    Walks the Borsh layout dynamically: ``monthly_spending_limit_members`` is a
    ``Vec<Pubkey>`` BEFORE the vault/mint fields, so no field after it sits at a
    fixed offset. Raises :class:`MetadaoStateError` on a foreign discriminator or
    truncated data — never fabricates.
    """
    if len(raw) < 8 or raw[:8] != _LAUNCH_DISCRIMINATOR:
        raise MetadaoStateError(
            "account data does not carry the Launch discriminator — wrong account "
            "(or the pre-resize OldLaunch layout)"
        )

    try:
        offset = 8 + 1  # discriminator + pda_bump
        minimum_raise_amount = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8 + 8  # minimum_raise + monthly_spending_limit_amount
        member_count = struct.unpack_from("<I", raw, offset)[0]
        offset += 4 + 32 * member_count  # Vec<Pubkey> — the dynamic prefix
        launch_authority = _pubkey(raw, offset)
        offset += 32
        launch_signer = _pubkey(raw, offset)
        offset += 32 + 1  # + launch_signer_pda_bump
        launch_quote_vault = _pubkey(raw, offset)
        offset += 32
        launch_base_vault = _pubkey(raw, offset)
        offset += 32
        base_mint = _pubkey(raw, offset)
        offset += 32
        quote_mint = _pubkey(raw, offset)
        offset += 32

        def _option_i64(at: int) -> tuple[int | None, int]:
            if raw[at]:
                return struct.unpack_from("<q", raw, at + 1)[0], at + 9
            return None, at + 1

        unix_timestamp_started, offset = _option_i64(offset)
        unix_timestamp_closed, offset = _option_i64(offset)
        total_committed_amount = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        state_index = raw[offset]
        offset += 1
        seq_num = struct.unpack_from("<Q", raw, offset)[0]
        offset += 8
        seconds_for_launch = struct.unpack_from("<I", raw, offset)[0]
    except (struct.error, IndexError) as exc:
        raise MetadaoStateError(
            f"Launch account data is {len(raw)} bytes — truncated mid-layout"
        ) from exc

    return LaunchAccountState(
        minimum_raise_amount=minimum_raise_amount,
        launch_authority=launch_authority,
        launch_signer=launch_signer,
        launch_quote_vault=launch_quote_vault,
        launch_base_vault=launch_base_vault,
        base_mint=base_mint,
        quote_mint=quote_mint,
        unix_timestamp_started=unix_timestamp_started,
        unix_timestamp_closed=unix_timestamp_closed,
        total_committed_amount=total_committed_amount,
        state_index=state_index,
        seq_num=seq_num,
        seconds_for_launch=seconds_for_launch,
    )


def read_launch_state(
    address: str,
    *,
    rpc_url: str = LOCAL_RPC,
    rpc_call: RpcCall | None = None,
) -> LaunchAccountState:
    """``getAccountInfo`` the launch and decode it. Raises
    :class:`MetadaoStateError` if the account is absent (a wrong ``base_mint``
    derives an address with no account — the wrong-launch guard)."""
    call = rpc_call or default_rpc_call
    resp = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not (
        isinstance(value, dict)
        and isinstance(value.get("data"), list)
        and value["data"]
        and isinstance(value["data"][0], str)
    ):
        raise MetadaoStateError(
            f"launch {address} not found on-chain — wrong base_mint, or the launch "
            "was never initialized"
        )
    return decode_launch_state(base64.b64decode(value["data"][0]))
