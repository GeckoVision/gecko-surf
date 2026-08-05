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

from .rpc import RpcCall
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


def assemble_unsigned_tx(instructions: Sequence[Any], fee_payer: str) -> BuiltTx:
    """Assemble an ordered list of instructions into a legacy UNSIGNED transaction.

    A placeholder (all-zero) blockhash is used because ``simulateTransaction`` is called
    with ``replaceRecentBlockhash:true`` — the Receipt therefore attests nothing about a
    land-time blockhash. The tx is ``new_unsigned`` (signature slots are all zero); it can
    ONLY be simulated, never sent. Returns a base64 :class:`BuiltTx`.
    """
    from solders.hash import Hash
    from solders.message import Message
    from solders.transaction import Transaction

    message = Message.new_with_blockhash(
        list(instructions), _pubkey(fee_payer), Hash.default()
    )
    unsigned = Transaction.new_unsigned(message)
    raw = bytes(unsigned)
    return BuiltTx(tx=base64.b64encode(raw).decode(), encoding="base64")


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
    postlude_ixs: Sequence[Any] = (),
) -> tuple[Receipt, int]:
    """Two-pass simulate ``[compute_budget..., *prelude_ixs, program_ix, *postlude_ixs]``.

    ``postlude_ixs`` run AFTER the program instruction — e.g. the wSOL ``CloseAccount``
    unwrap of a SOL-leg swap. Pass 1 uses a generous CU limit to learn
    ``units_consumed``; pass 2 sets the limit to ``units × 1.2`` (+ the priority-fee
    price if given) and returns that Receipt plus the chosen limit. If pass 1 does not
    pass (or reports no units), it is returned as-is with the max limit — the honest
    verdict, no second guess. ``simulateTransaction`` only; the unsigned tx is never
    sent.
    """
    kwargs: dict[str, Any] = {"rpc_url": rpc_url, "rpc_call": rpc_call, "track": track}
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
