"""Assemble the STANDARD landing preludes around a built program instruction — for the
$0 SIMULATION ONLY.

This module is the compose boundary in code. Orquestra builds the program instruction (the
IDL-hard part — e.g. Pump's ``buy``); Gecko wraps it with the two STANDARD, public preludes
that turn a derived account set into a tx that actually lands:

  * ``createAssociatedTokenAccountIdempotent`` — so the buyer's ATA exists (kills the
    AnchorError 3012 ``associated_user`` revert);
  * ``ComputeBudget`` ``SetComputeUnitLimit`` / ``SetComputeUnitPrice`` — so the tx has a
    CU budget and a priority fee.

Gecko assembles these ONLY to produce an UNSIGNED message for ``simulateTransaction``
(``sigVerify:false, replaceRecentBlockhash:true``) — that is VERIFICATION, not signing.
**This module never signs, never ``sendTransaction``, never broadcasts, holds no keypair.**
The one transaction it produces is unsigned (all-zero signature slots), fit only to be
simulated. The REAL, signable tx is built by Orquestra and signed by 1claw — Gecko hands
them the *declared* ordered plan (see ``pumpfun_landing.landing_plan``).

solders (the optional ``[solana]`` extra) is imported lazily, mirroring :mod:`gecko.pda`.
"""

from __future__ import annotations

import base64
import struct
from typing import Any, Mapping, Sequence

from .events import emit_surf_event
from .networks import UNKNOWN_NETWORK, Network
from .rpc import RpcCall, default_rpc_call
from .simulate import BuiltTx, Receipt, simulate

__all__ = [
    "ASSOCIATED_TOKEN_PROGRAM_ID",
    "COMPUTE_BUDGET_PROGRAM_ID",
    "NATIVE_SOL_MINT",
    "SYSTEM_PROGRAM_ID",
    "TOKEN_2022_PROGRAM_ID",
    "TOKEN_PROGRAM_ID",
    "LandingError",
    "assemble_unsigned_tx",
    "close_account_ix",
    "compute_budget_ixs",
    "create_idempotent_ata_ix",
    "orquestra_instruction_to_solders",
    "simulate_landing_bundle",
    "with_remaining_accounts",
    "with_writable_accounts",
    "wrap_sol_ixs",
]

COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# The native-SOL sentinel mint: a pool/instruction whose leg is this mint moves SOL as
# wrapped SOL, so the landing bundle needs wrap (transfer + SyncNative) / CloseAccount.
NATIVE_SOL_MINT = "So11111111111111111111111111111111111111112"

# ComputeBudget instruction discriminators (first data byte).
_CU_SET_LIMIT = 2  # SetComputeUnitLimit(u32 units)
_CU_SET_PRICE = 3  # SetComputeUnitPrice(u64 micro-lamports)
# Associated-Token-Account program instruction discriminator.
_ATA_CREATE_IDEMPOTENT = 1  # CreateIdempotent (0 = Create, 2 = RecoverNested)
# System program instruction index (u32 LE).
_SYSTEM_TRANSFER = 2  # Transfer { lamports: u64 }
# SPL-Token program instruction discriminators (first data byte).
_TOKEN_CLOSE_ACCOUNT = 9  # CloseAccount
_TOKEN_SYNC_NATIVE = 17  # SyncNative

# The default max CU limit for the first (learning) pass — Solana's per-tx cap. A bare
# probe sim reports units_consumed; the second pass sets the real limit from that.
_MAX_UNIT_LIMIT = 1_400_000
_CU_MARGIN = 1.2  # headroom over measured units, so the real send doesn't clip


class LandingError(Exception):
    """A prelude-assembly failure — a malformed built instruction, a bad pubkey, or a
    missing solders extra. Messages carry only public data (program ids, offsets) — never a
    secret; assembling public instructions has no secret material by construction."""


def _pubkey(value: str):  # type: ignore[no-untyped-def]
    """Parse a base58 pubkey via solders (lazy import behind the [solana] extra)."""
    try:
        from solders.pubkey import Pubkey
    except ImportError as exc:  # pragma: no cover - needs the [solana] extra
        raise LandingError(
            "assembling landing preludes needs the 'solana' extra: install with "
            "`pip install gecko-surf[solana]` (or `uv add solders`)"
        ) from exc
    try:
        return Pubkey.from_string(value)
    except Exception as exc:
        raise LandingError(f"{value!r} is not a valid base58 pubkey") from exc


def compute_budget_ixs(unit_limit: int, unit_price_microlamports: int = 0) -> list[Any]:
    """The ComputeBudget preludes: a ``SetComputeUnitLimit`` and (if a non-zero price is
    given) a ``SetComputeUnitPrice``. Standard, public instructions — no accounts."""
    from solders.instruction import Instruction

    program = _pubkey(COMPUTE_BUDGET_PROGRAM_ID)
    ixs: list[Any] = [
        Instruction(program, bytes([_CU_SET_LIMIT]) + struct.pack("<I", unit_limit), [])
    ]
    if unit_price_microlamports > 0:
        ixs.append(
            Instruction(
                program,
                bytes([_CU_SET_PRICE]) + struct.pack("<Q", unit_price_microlamports),
                [],
            )
        )
    return ixs


def create_idempotent_ata_ix(
    payer: str, owner: str, mint: str, ata: str, token_program: str
) -> Any:
    """The Associated-Token-Account program's ``CreateIdempotent`` — creates ``ata`` for
    ``(owner, mint)`` if it does not already exist, a no-op if it does. This is THE prelude
    that removes AnchorError 3012. ``token_program`` must match the mint's program (Token
    vs Token-2022 — the value ``plan_buy`` resolves from the mint owner).
    """
    from solders.instruction import AccountMeta, Instruction

    metas = [
        AccountMeta(_pubkey(payer), True, True),  # funding account (signer, writable)
        AccountMeta(_pubkey(ata), False, True),  # ATA to create (writable)
        AccountMeta(_pubkey(owner), False, False),  # wallet / owner
        AccountMeta(_pubkey(mint), False, False),
        AccountMeta(_pubkey(SYSTEM_PROGRAM_ID), False, False),
        AccountMeta(_pubkey(token_program), False, False),
    ]
    return Instruction(
        _pubkey(ASSOCIATED_TOKEN_PROGRAM_ID),
        bytes([_ATA_CREATE_IDEMPOTENT]),
        metas,
    )


def wrap_sol_ixs(owner: str, wsol_ata: str, lamports: int) -> list[Any]:
    """The wSOL wrap prelude pair: a System ``Transfer`` of ``lamports`` into the
    owner's wSOL ATA, then SPL-Token ``SyncNative`` so the token balance reflects
    them. Standard public instructions; the ATA itself is created separately with
    :func:`create_idempotent_ata_ix` (native SOL is always classic SPL-Token, never
    Token-2022). Raises on a non-positive amount — a zero wrap is a caller bug.
    """
    from solders.instruction import AccountMeta, Instruction

    if lamports <= 0:
        raise LandingError(f"wrap_sol_ixs needs a positive lamports, got {lamports}")
    transfer = Instruction(
        _pubkey(SYSTEM_PROGRAM_ID),
        struct.pack("<IQ", _SYSTEM_TRANSFER, lamports),
        [
            AccountMeta(_pubkey(owner), True, True),  # funding account (signer)
            AccountMeta(_pubkey(wsol_ata), False, True),
        ],
    )
    sync_native = Instruction(
        _pubkey(TOKEN_PROGRAM_ID),
        bytes([_TOKEN_SYNC_NATIVE]),
        [AccountMeta(_pubkey(wsol_ata), False, True)],
    )
    return [transfer, sync_native]


def close_account_ix(account: str, destination: str, owner: str) -> Any:
    """SPL-Token ``CloseAccount`` — closes ``account`` and sends its lamports (for a
    wSOL ATA: the unwrapped SOL plus the rent) to ``destination``. The unwrap half of
    a SOL-leg landing bundle, placed AFTER the program instruction."""
    from solders.instruction import AccountMeta, Instruction

    metas = [
        AccountMeta(_pubkey(account), False, True),
        AccountMeta(_pubkey(destination), False, True),
        AccountMeta(_pubkey(owner), True, False),  # close authority (signer)
    ]
    return Instruction(_pubkey(TOKEN_PROGRAM_ID), bytes([_TOKEN_CLOSE_ACCOUNT]), metas)


def orquestra_instruction_to_solders(instruction: Mapping[str, Any]) -> Any:
    """Turn Orquestra ``/build``'s ``instruction`` object into a solders ``Instruction``.

    Orquestra returns ``{name, programId, data, accounts}`` where ``data`` is a HEX string
    and each account carries ``{pubkey, isSigner, isWritable}``. This is the built program
    instruction (Orquestra's lane); Gecko only re-wraps it for the sim. Raises
    :class:`LandingError` on a malformed object — never fabricates.
    """
    from solders.instruction import AccountMeta, Instruction

    program_id = instruction.get("programId")
    data_hex = instruction.get("data")
    accounts = instruction.get("accounts")
    if not (
        isinstance(program_id, str)
        and isinstance(data_hex, str)
        and isinstance(accounts, list)
    ):
        raise LandingError(
            "orquestra instruction must carry programId (str), data (hex str) and "
            "accounts (list)"
        )
    try:
        data = bytes.fromhex(data_hex)
    except ValueError as exc:
        raise LandingError("orquestra instruction data is not valid hex") from exc

    metas = []
    for account in accounts:
        pubkey = account.get("pubkey") if isinstance(account, Mapping) else None
        if not isinstance(pubkey, str):
            raise LandingError("orquestra instruction account is missing a pubkey")
        metas.append(
            AccountMeta(
                _pubkey(pubkey),
                bool(account.get("isSigner")),
                bool(account.get("isWritable")),
            )
        )
    return Instruction(_pubkey(program_id), data, metas)


def with_remaining_accounts(
    instruction: Any, remaining: Sequence[tuple[str, bool]]
) -> Any:
    """Return a copy of ``instruction`` with extra remaining accounts appended (in order).

    ``remaining`` is ``(pubkey, is_writable)`` pairs, all non-signer — the accounts an
    Anchor IDL drops because they only travel as ``remaining_accounts`` (Gecko RECOVERS
    them from source/state, e.g. Pump's ``bonding_curve_v2`` + the 8 buyback fee recipients).
    Only extends the account list; the program id and data are untouched.
    """
    from solders.instruction import AccountMeta, Instruction

    extra = [
        AccountMeta(_pubkey(pubkey), False, bool(writable))
        for pubkey, writable in remaining
    ]
    return Instruction(
        instruction.program_id,
        bytes(instruction.data),
        list(instruction.accounts) + extra,
    )


def with_writable_accounts(
    instruction: Any, writable: Sequence[str]
) -> tuple[Any, tuple[str, ...]]:
    """Return ``(instruction, corrected)`` with every account in ``writable`` marked
    writable, plus the accounts whose flag actually changed.

    The comprehension gap this closes: an IDL can declare an account read-only that the
    DEPLOYED program passes on to a CPI as writable. Solana rejects that at the inner
    invoke ("writable privilege escalated"), *after* the instruction has already done its
    work — so it is invisible to anything short of a simulation. Gecko recovers the true
    metas from source and reconciles them here.

    Deliberately narrow: it only WIDENS writability. It never adds ``is_signer`` — Gecko
    does not get to decide who signs — and never touches the program id or the data.
    """
    from solders.instruction import AccountMeta, Instruction

    wanted = set(writable)
    corrected: list[str] = []
    metas = []
    for meta in instruction.accounts:
        key = str(meta.pubkey)
        if key in wanted and not meta.is_writable:
            corrected.append(key)
            metas.append(AccountMeta(meta.pubkey, meta.is_signer, True))
        else:
            metas.append(meta)
    return (
        Instruction(instruction.program_id, bytes(instruction.data), metas),
        tuple(corrected),
    )


def fetch_lookup_tables(
    addresses: Sequence[str], *, rpc_url: str, rpc_call: RpcCall | None = None
) -> list[Any]:
    """Read address-lookup tables from chain so a v0 message can resolve against them.

    An ALT is what lets a transaction reference more accounts than a legacy message can
    hold. An aggregator hands back the TABLE ADDRESSES, not their contents, so the
    addresses inside have to be read before anything can be compiled — which is another
    instance of the same lesson this repo keeps meeting: the surface names a pointer, and
    the value lives somewhere else.

    A table that cannot be read is an error, never a silent omission: compiling without it
    would produce a transaction that references accounts the runtime cannot resolve.
    """
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.pubkey import Pubkey

    call = rpc_call or default_rpc_call
    tables: list[Any] = []
    for address in addresses:
        response = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
        value = (response.get("result") or {}).get("value")
        if not (isinstance(value, dict) and isinstance(value.get("data"), list)):
            raise LandingError(f"lookup table {address[:8]}… could not be read")
        raw = base64.b64decode(value["data"][0])
        # Layout: 56-byte header, then a packed array of 32-byte addresses.
        body = raw[56:]
        if len(body) % 32:
            raise LandingError(
                f"lookup table {address[:8]}… has a ragged address array"
            )
        tables.append(
            AddressLookupTableAccount(
                key=Pubkey.from_string(address),
                addresses=[
                    Pubkey.from_bytes(body[i : i + 32]) for i in range(0, len(body), 32)
                ],
            )
        )
    return tables


def assemble_unsigned_tx(
    instructions: Sequence[Any],
    fee_payer: str,
    *,
    blockhash: str | None = None,
    lookup_tables: Sequence[Any] = (),
) -> BuiltTx:
    """Assemble instructions into an UNSIGNED transaction — legacy, or v0 when needed.

    ``blockhash`` defaults to the all-zero placeholder, because ``simulateTransaction`` is
    called with ``replaceRecentBlockhash:true`` and a Receipt therefore attests nothing
    about a land-time blockhash. Pass a REAL blockhash (and simulate without replacement)
    when the receipt needs to bind one — that is the difference between a `structural` and
    an `exact` binding, and it starts expiring the moment it is fetched.

    ``lookup_tables`` switches the output to a **versioned (v0)** message. A legacy message
    caps out around 35 accounts and 1232 bytes; a multi-hop aggregator route exceeds that
    routinely, so anything carrying tables must be compiled as v0 or it simply will not fit.

    The transaction is unsigned — signature slots are all zero — so it can ONLY be
    simulated, never sent. Returns a base64 :class:`BuiltTx`.
    """
    from solders.hash import Hash
    from solders.message import Message, MessageV0
    from solders.null_signer import NullSigner
    from solders.transaction import Transaction, VersionedTransaction

    recent = Hash.from_string(blockhash) if blockhash else Hash.default()
    payer = _pubkey(fee_payer)

    if lookup_tables:
        message = MessageV0.try_compile(
            payer, list(instructions), list(lookup_tables), recent
        )
        # NullSigner keeps the signature slot present but zeroed: the shape a signer
        # expects, with nothing signed. Gecko holds no key and produces none.
        unsigned = VersionedTransaction(message, [NullSigner(payer)])
    else:
        legacy = Message.new_with_blockhash(list(instructions), payer, recent)
        unsigned = Transaction.new_unsigned(legacy)  # type: ignore[assignment]

    return BuiltTx(tx=base64.b64encode(bytes(unsigned)).decode(), encoding="base64")


def latest_blockhash(rpc_url: str, rpc_call: RpcCall | None = None) -> tuple[str, int]:
    """The current blockhash and its last valid block height.

    Returned as a pair so a caller can reason about the deadline rather than discovering
    it at send time: a blockhash is valid for roughly 150 slots (about a minute), and that
    window is the clock on the whole verify-then-sign sequence.

    COMMITMENT IS `confirmed`, AND THE CHOICE IS WORTH ~14 SECONDS. This read `finalized`,
    which is ~31-35 blocks behind the tip. The transaction's deadline is
    ``lastValidBlockHeight`` measured against the chain's ACTUAL height at submit time, so
    a finalized blockhash spends a fifth of the 150-block budget before the caller ever
    sees the bytes. Measured on mainnet: finalized left 114 blocks (~46s) where confirmed
    left 149 (~60s) at the same instant.

    That is the difference between a window that fits a cold loop and one that does not —
    the first real PayBox purchase died on ``BlockhashNotFound`` because the client was
    still loading tool definitions between prepare and sign.

    `confirmed` is what wallets and dApps use for exactly this call. Its failure mode is
    also the benign one: if a confirmed block were forked away the transaction simply
    would not land, which is a retry, not a wrong outcome — and re-running prepare is free.
    """
    call = rpc_call or default_rpc_call
    response = call(rpc_url, "getLatestBlockhash", [{"commitment": RPC_COMMITMENT}])
    value = (response.get("result") or {}).get("value") or {}
    blockhash = value.get("blockhash")
    height = value.get("lastValidBlockHeight")
    if not isinstance(blockhash, str) or not isinstance(height, int):
        raise LandingError("RPC returned no usable blockhash")
    return blockhash, height


#: THE commitment this whole path works at — one definition, every caller imports it.
#:
#: It exists because the two sides drifted and cost a live purchase. `latest_blockhash`
#: moved to `confirmed` to reclaim ~14s of window, which makes the blockhash ~35 blocks
#: NEWER than finalized; `sendTransaction`'s preflight defaults to `finalized` and
#: therefore did not know it yet, so every broadcast failed with `BlockhashNotFound` while
#: the simulation passed. Two commitments, one of them implicit, is how that happens.
#:
#: THE RULE, and it is an ordering rather than an equality: whatever validates a
#: transaction must be at least as LOOSE as whatever issued its blockhash —
#: `processed` ⊇ `confirmed` ⊇ `finalized`. Simulation deliberately uses `processed`
#: (looser, sees the newest state); the preflight must never be stricter than this.
RPC_COMMITMENT = "confirmed"


def block_height(rpc_url: str, rpc_call: RpcCall | None = None) -> int | None:
    """The chain's current block height at ``confirmed``, or ``None``.

    Paired with ``lastValidBlockHeight`` this is what turns a deadline into a BUDGET: the
    difference is how many blocks a signer has left, and ~0.4s each converts it to seconds.
    Read at the same commitment as the blockhash so the two are comparable.

    Returns ``None`` rather than raising: this is an ergonomic extra on a path whose real
    job already succeeded, and losing the countdown must never cost the caller a
    transaction that would otherwise have landed.
    """
    call = rpc_call or default_rpc_call
    try:
        response = call(rpc_url, "getBlockHeight", [{"commitment": RPC_COMMITMENT}])
    except Exception:  # noqa: BLE001 - see docstring; the plan stands without the clock
        return None
    height = response.get("result")
    return height if isinstance(height, int) else None


def priority_fee_microlamports(
    accounts: Sequence[str],
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    percentile: float = 0.75,
    floor_microlamports: int = 0,
) -> int:
    """A priority fee derived from what the market actually clears at, floored.

    Our default of zero is correct for simulation and wrong for mainnet: under contention a
    zero-priority transaction may simply never land, and "the simulation passed" says
    nothing about whether a validator will pick it up. Best-effort — an RPC that does not
    answer yields the floor rather than blocking the caller, since a missing fee estimate
    must not look like a failed plan.

    MEASURED 2026-08-31 (7 of 12 broadcasts expired unspent before this changed):
    ``getRecentPrioritizationFees`` reports per-slot MINIMUM fees — 0 on 150 of 150 recent
    slots for every account set we asked about — while the same node's
    ``getPriorityFeeEstimate`` put the market's "medium" at ~3,300 μlam/CU. So this asks
    the richer estimator first (a provider extension: a node that lacks it just errors and
    we fall through), then the per-slot minimums, and finally applies ``floor_microlamports``
    so a mainnet caller can refuse to bid zero into a market that is not free.
    """
    call = rpc_call or default_rpc_call
    estimate = 0
    try:
        response = call(
            rpc_url,
            "getPriorityFeeEstimate",
            [{"accountKeys": list(accounts), "options": {"recommended": True}}],
        )
        result = response.get("result") or {}
        fee = result.get("priorityFeeEstimate")
        if isinstance(fee, (int, float)) and fee > 0:
            estimate = int(fee)
    except Exception:  # noqa: BLE001 - a provider extension; absence is normal
        estimate = 0
    if estimate <= 0:
        try:
            response = call(rpc_url, "getRecentPrioritizationFees", [list(accounts)])
            fees = [
                entry["prioritizationFee"]
                for entry in (response.get("result") or [])
                if isinstance(entry, dict)
                and isinstance(entry.get("prioritizationFee"), int)
            ]
            paying = sorted(fee for fee in fees if fee > 0)
            if paying:
                index = min(len(paying) - 1, int(len(paying) * percentile))
                estimate = int(paying[index])
        except Exception:  # noqa: BLE001 - an estimate is an enrichment, never a gate
            estimate = 0
    return max(estimate, floor_microlamports)


def simulate_landing_bundle(
    program_ix: Any,
    prelude_ixs: Sequence[Any],
    fee_payer: str,
    *,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
    unit_price_microlamports: int = 0,
    track: Sequence[str] = (),
    network_label: str | None = None,
    network: Network = UNKNOWN_NETWORK,
    postlude_ixs: Sequence[Any] = (),
    program: str | None = None,
    instruction: str | None = None,
) -> tuple[Receipt, int]:
    """Two-pass simulate ``[compute_budget..., *prelude_ixs, program_ix, *postlude_ixs]``.

    ``postlude_ixs`` run AFTER the program instruction — e.g. the wSOL ``CloseAccount``
    unwrap of a SOL-leg swap. Pass 1 uses a generous CU limit to learn
    ``units_consumed``; pass 2 sets the limit to ``units × 1.2`` (+ the priority-fee
    price if given) and returns that Receipt plus the chosen limit. If pass 1 does not
    pass (or reports no units), it is returned as-is with the max limit — the honest
    verdict, no second guess. ``simulateTransaction`` only; the unsigned tx is never
    sent.

    ``network`` is passed THROUGH to the Receipt and is never decided here. This function
    is handed an ``rpc_url`` and an injected transport and is told nothing about either,
    so the only value it could honestly invent is the fail-closed one — which is exactly
    what it defaults to. A caller that WAS told (an operator flag) threads the real member
    down; every other caller gets ``unknown``, which approves nothing at the signing gate.
    Hardcoding an approvable member here would be a fabricated assertion, and
    ``tests/test_network_vocabulary.py`` lints this file for precisely that.
    """
    # The Program Surface was previously invisible to usage telemetry: every event kind
    # we emitted came from the HTTP/MCP path, so "an agent planned a real Solana call"
    # produced no record at all. One emit here covers all four programs, since every
    # landing orchestrator routes through this function.
    #
    # It emits ``prepare`` ("we assembled a complete call") and deliberately NOT
    # ``first_call_correct``: a fork simulation passing is not the same claim as a real
    # first call succeeding, and the simulated outcome already has an honest home in the
    # corpus's ``simulated`` tier. Overstating here would corrupt the one metric we can
    # defend.
    if program and instruction:
        emit_surf_event(
            "surf.prepare",
            surface_id=program,
            tool_name=instruction,
            plane="surface",
        )

    kwargs: dict[str, Any] = {
        "rpc_url": rpc_url,
        "rpc_call": rpc_call,
        "track": track,
        "network": network,
    }
    if network_label is not None:
        kwargs["network_label"] = network_label

    first_ixs = [
        *compute_budget_ixs(_MAX_UNIT_LIMIT),
        *prelude_ixs,
        program_ix,
        *postlude_ixs,
    ]
    first = simulate(
        {},
        build_call=lambda _plan: assemble_unsigned_tx(first_ixs, fee_payer),
        **kwargs,
    )
    if first.status != "pass" or first.units_consumed is None:
        return first, _MAX_UNIT_LIMIT

    limit = min(int(first.units_consumed * _CU_MARGIN), _MAX_UNIT_LIMIT)
    second_ixs = [
        *compute_budget_ixs(limit, unit_price_microlamports),
        *prelude_ixs,
        program_ix,
        *postlude_ixs,
    ]
    second = simulate(
        {},
        build_call=lambda _plan: assemble_unsigned_tx(second_ixs, fee_payer),
        **kwargs,
    )
    return second, limit
