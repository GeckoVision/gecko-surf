"""Privy's policy vocabulary, and the pieces our own pre-sign re-check shares.

Two wallets are restricted by a Privy policy (the user's run-payment wallet,
:mod:`gecko.anyquery.wallet_policy`; Gecko's settlement wallet,
:mod:`gecko.anyquery.settlement_policy`). Both are written from the words below, and both
are re-checked in our process over the exact bytes before Privy is asked.

**CONFIRMED, 9 Oct (founder's first live create).** Privy echoed our policy back with
these names intact: a rule carries ``method: signTransaction`` and ``action: ALLOW``; a
condition carries ``field_source`` / ``field`` / ``operator`` / ``value``; the sources
``solana_token_program_instruction`` and ``solana_system_program_instruction``; the
fields ``instructionName``, ``TransferChecked.destination`` / ``.mint`` / ``.amount``,
``Transfer.to`` / ``.lamports``; the operators ``eq`` and ``lte``. That is
:data:`POLICY_VOCABULARY`. An echo proves Privy STORED the words; it does not prove the
enclave evaluates them the way we read them, which only a denied signature can show.

**UNCONFIRMED, still.** Nothing in this repo has sent these and seen them echoed:

* :data:`UNCONFIRMED_PROGRAM_VOCABULARY` - a rule on a whole program
  (``solana_program_instruction`` / ``programId``), from Privy's Solana examples. It is
  the only way a Whirlpool swap can be named at all: Privy has no field that reads a
  Whirlpool instruction's accounts, so it cannot bind the pool or the output account.
* default-deny: that an instruction matching no ALLOW rule makes Privy refuse the whole
  transaction. Both policies rely on it and send no DENY rule.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from ..spend_policy import SpendRefusalCode

__all__ = [
    "POLICY_VOCABULARY",
    "UNCONFIRMED_PROGRAM_VOCABULARY",
    "Instruction",
    "PolicyRefusal",
    "allow_rule",
    "condition",
    "decode_instructions",
    "policy_body",
    "program_rule",
    "sol_transfer_rule",
    "usdc_transfer_rule",
]

#: Confirmed by Privy's echo of the first live policy (9 Oct).
POLICY_VOCABULARY: dict[str, str] = {
    "version": "1.0",
    "method": "signTransaction",
    "allow": "ALLOW",
    "token_source": "solana_token_program_instruction",
    "system_source": "solana_system_program_instruction",
    "instruction_name": "instructionName",
    "transfer_checked": "TransferChecked",
    "transfer_checked_destination": "TransferChecked.destination",
    "transfer_checked_mint": "TransferChecked.mint",
    "transfer_checked_amount": "TransferChecked.amount",
    "system_transfer": "Transfer",
    "system_transfer_to": "Transfer.to",
    "system_transfer_lamports": "Transfer.lamports",
    "eq": "eq",
    "lte": "lte",
}

#: UNCONFIRMED: a rule over a whole program. From Privy's Solana policy examples; never
#: sent and echoed by this repo. TODO(web3-engineer): the settlement wallet's create is
#: the first live use; read the echo back before its first settlement.
UNCONFIRMED_PROGRAM_VOCABULARY: dict[str, str] = {
    "program_source": "solana_program_instruction",
    "program_id": "programId",
}

#: Privy's token-program vocabulary (docs, 9 Oct) names only Transfer, TransferChecked,
#: Burn, MintTo, CloseAccount and InitializeAccount3. ``SyncNative`` is NOT one of them: a
#: live policy create that named it answered HTTP 400. Never put it in a rule.


def condition(source: str, field: str, operator: str, value: str) -> dict[str, str]:
    """One condition, from words already looked up (never a raw caller string)."""
    return {
        "field_source": source,
        "field": field,
        "operator": operator,
        "value": value,
    }


def allow_rule(name: str, conditions: list[dict[str, str]]) -> dict[str, Any]:
    v = POLICY_VOCABULARY
    return {
        "name": name,
        "method": v["method"],
        "conditions": conditions,
        "action": v["allow"],
    }


def policy_body(name: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    """A whole policy. ALLOW rules only: an instruction no rule matches is denied."""
    return {
        "version": POLICY_VOCABULARY["version"],
        "name": name,
        "chain_type": "solana",
        "rules": rules,
    }


def usdc_transfer_rule(name: str, *, destination: str, mint: str, cap: int) -> dict:
    """``TransferChecked`` of ``mint`` to ``destination`` only, at most ``cap``."""
    v = POLICY_VOCABULARY
    src = v["token_source"]
    return allow_rule(
        name,
        [
            condition(src, v["instruction_name"], v["eq"], v["transfer_checked"]),
            condition(src, v["transfer_checked_destination"], v["eq"], destination),
            condition(src, v["transfer_checked_mint"], v["eq"], mint),
            condition(src, v["transfer_checked_amount"], v["lte"], str(cap)),
        ],
    )


def sol_transfer_rule(name: str, *, to: str, cap: int) -> dict[str, Any]:
    """System ``Transfer`` to ``to`` only, at most ``cap`` lamports."""
    v = POLICY_VOCABULARY
    src = v["system_source"]
    return allow_rule(
        name,
        [
            condition(src, v["instruction_name"], v["eq"], v["system_transfer"]),
            condition(src, v["system_transfer_to"], v["eq"], to),
            condition(src, v["system_transfer_lamports"], v["lte"], str(cap)),
        ],
    )


def program_rule(name: str, program_id: str) -> dict[str, Any]:
    """Every instruction of ``program_id``. UNCONFIRMED words, and as wide as it reads:
    it binds no account and no amount. Only the settlement wallet carries one."""
    u = UNCONFIRMED_PROGRAM_VOCABULARY
    return allow_rule(
        name,
        [
            condition(
                u["program_source"],
                u["program_id"],
                POLICY_VOCABULARY["eq"],
                program_id,
            )
        ],
    )


# --------------------------------------------------------------------------------------
# The local re-check's shared pieces
# --------------------------------------------------------------------------------------


class PolicyRefusal(Exception):
    """One instruction the local check refuses, with the spend gate's refusal code."""

    def __init__(self, code: SpendRefusalCode, reason: str) -> None:
        super().__init__(reason)
        self.code: SpendRefusalCode = code
        self.reason = reason


@dataclass(frozen=True)
class Instruction:
    """``(program, data, accounts)`` of one instruction, read from the bytes alone."""

    program: str
    data: bytes
    accounts: list[str]


def decode_instructions(transaction: str | bytes) -> list[Instruction]:
    """Every instruction of a legacy or v0 message. A message that loads accounts from a
    lookup table is refused: its destinations are not in the bytes."""
    raw = (
        transaction
        if isinstance(transaction, bytes)
        else base64.b64decode(transaction, validate=True)
    )
    from solders.transaction import Transaction, VersionedTransaction

    try:
        message: Any = VersionedTransaction.from_bytes(raw).message
    except Exception:  # noqa: BLE001 - try the legacy wire format next
        message = Transaction.from_bytes(raw).message
    if list(getattr(message, "address_table_lookups", None) or ()):
        raise PolicyRefusal(
            "undecodable-transaction",
            "the message loads accounts from a lookup table, so its destinations are "
            "not in the bytes",
        )
    keys = [str(k) for k in message.account_keys]
    out = [
        Instruction(
            keys[ix.program_id_index],
            bytes(ix.data),
            [keys[i] for i in bytes(ix.accounts)],
        )
        for ix in message.instructions
    ]
    if not out:
        raise PolicyRefusal(
            "undecodable-transaction", "the transaction has no instructions"
        )
    return out
