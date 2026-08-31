"""Priority-fee injection for a builder's unsigned transaction — a VERIFIED rebuild.

WHY THIS EXISTS. On 2026-08-31, 7 of 12 mainnet broadcasts expired unspent. The cause
was measured, not guessed: ``getRecentPrioritizationFees`` reports per-slot MINIMUM
fees, which are 0 on nearly every slot, so the percentile estimator bid 0 microlamports
into a market whose real clearing price (the same node's ``getPriorityFeeEstimate``)
was ~3,300 μlam/CU at "medium". A zero-fee transaction lands on leader luck.

WHY IT IS SHAPED LIKE THIS. The purchase loop's rule is substitution, not re-assembly
(see ``_with_fresh_blockhash``): the builder owns the instruction list. Adding a
compute-budget instruction cannot be a byte patch — the account table and instruction
array both change — so this module does the one re-assembly the rule allows: rebuild,
then PROVE the rebuild preserved the builder's program semantically. It fails closed on
any difference: every original instruction must survive with the same program id, the
same account metas in the same order (pubkey, signer, writable), and the same data, the
fee payer and blockhash must be unchanged, and the only addition must be the one
compute-budget instruction this module prepended. A rebuild that cannot be proven
equivalent raises; nothing downstream ever sees it.

The caller then simulates the EXACT new bytes and binds the receipt to them (`exact`),
so the signature still covers precisely what was verified — the injection happens
before the receipt, never after.

Versioned (v0) messages are refused rather than mishandled: address-table lookups make
decompilation non-local, and a wrong guess here would be signed. The builder this loop
talks to emits legacy messages; if that changes, this module must learn v0 first.
"""

from __future__ import annotations

import base64

from solders.compute_budget import set_compute_unit_price
from solders.instruction import AccountMeta, CompiledInstruction, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

__all__ = ["FeebumpError", "with_priority_fee"]

COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)
#: First data byte of the SetComputeUnitPrice variant of the ComputeBudget program.
_SET_UNIT_PRICE_TAG = 3


class FeebumpError(Exception):
    """The unsigned transaction could not be safely rebuilt with a priority fee.

    Raised instead of returning anything: a caller that receives this must decide
    explicitly whether to proceed WITHOUT a fee (the transaction is untouched) —
    silently signing an unproven rebuild is the failure mode this type exists to
    prevent.
    """


def _decompile(message: Message) -> list[Instruction]:
    """The message's instructions as (program, metas, data) — layout rules, no guessing.

    Writability and signer-ness are DERIVED from the message header exactly as the
    runtime derives them; getting either wrong would change what the rebuilt message
    asks the signer to authorise, which is why this refuses anything but a legacy
    ``Message`` (the caller has already rejected v0).
    """
    keys = list(message.account_keys)
    header = message.header
    signers = header.num_required_signatures
    readonly_signed = header.num_readonly_signed_accounts
    readonly_unsigned = header.num_readonly_unsigned_accounts

    def meta(index: int) -> AccountMeta:
        is_signer = index < signers
        if is_signer:
            is_writable = index < signers - readonly_signed
        else:
            is_writable = index < len(keys) - readonly_unsigned
        return AccountMeta(keys[index], is_signer=is_signer, is_writable=is_writable)

    instructions: list[Instruction] = []
    for compiled in message.instructions:
        assert isinstance(compiled, CompiledInstruction)
        instructions.append(
            Instruction(
                keys[compiled.program_id_index],
                bytes(compiled.data),
                [meta(i) for i in bytes(compiled.accounts)],
            )
        )
    return instructions


def _same_instruction(left: Instruction, right: Instruction) -> bool:
    if left.program_id != right.program_id or bytes(left.data) != bytes(right.data):
        return False
    if len(left.accounts) != len(right.accounts):
        return False
    return all(
        a.pubkey == b.pubkey
        and a.is_signer == b.is_signer
        and a.is_writable == b.is_writable
        for a, b in zip(left.accounts, right.accounts, strict=True)
    )


def with_priority_fee(unsigned_transaction_base64: str, microlamports: int) -> str:
    """The builder's transaction with one SetComputeUnitPrice prepended, proven intact.

    ``microlamports <= 0`` returns the input unchanged — an explicit "no bid" is not an
    error. A transaction that already carries a SetComputeUnitPrice is returned
    unchanged too: the builder (or a human) priced it deliberately, and out-bidding
    them here would make this module the authority the docstring says it is not.
    """
    if microlamports <= 0:
        return unsigned_transaction_base64

    raw = base64.b64decode(unsigned_transaction_base64)
    try:
        original = Transaction.from_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - solders raises several types here
        raise FeebumpError(
            f"not a legacy transaction this module can rebuild ({type(exc).__name__}); "
            "versioned messages are refused rather than mishandled"
        ) from None
    message = original.message

    instructions = _decompile(message)
    for instruction in instructions:
        if instruction.program_id == COMPUTE_BUDGET_PROGRAM_ID and bytes(
            instruction.data
        )[:1] == bytes([_SET_UNIT_PRICE_TAG]):
            return unsigned_transaction_base64

    fee_payer = message.account_keys[0]
    rebuilt_message = Message.new_with_blockhash(
        [set_compute_unit_price(microlamports), *instructions],
        fee_payer,
        message.recent_blockhash,
    )

    # THE PROOF. Decompile what was just built and require the builder's program back,
    # unchanged, behind exactly one prepended compute-budget instruction.
    rebuilt_instructions = _decompile(rebuilt_message)
    if len(rebuilt_instructions) != len(instructions) + 1:
        raise FeebumpError("the rebuild changed the instruction count beyond the fee")
    added = rebuilt_instructions[0]
    if added.program_id != COMPUTE_BUDGET_PROGRAM_ID or bytes(added.data) != bytes(
        set_compute_unit_price(microlamports).data
    ):
        raise FeebumpError(
            "the rebuild's first instruction is not the fee that was set"
        )
    for index, (ours, theirs) in enumerate(
        zip(rebuilt_instructions[1:], instructions, strict=True)
    ):
        if not _same_instruction(ours, theirs):
            raise FeebumpError(
                f"instruction {index} did not survive the rebuild identically; "
                "refusing to hand these bytes onward"
            )
    if rebuilt_message.account_keys[0] != fee_payer:
        raise FeebumpError("the rebuild changed the fee payer")
    if rebuilt_message.recent_blockhash != message.recent_blockhash:
        raise FeebumpError("the rebuild changed the blockhash")

    return base64.b64encode(bytes(Transaction.new_unsigned(rebuilt_message))).decode()
