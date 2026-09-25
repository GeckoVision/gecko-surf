"""``make_purchase``, built HERE — this program's ABI as data, plus one wiring line.

Why this module exists. Until 2026-09-25 the purchase path built its one instruction by
POSTing the resolved plan to Orquestra ``/build``. That day the endpoint answered
``HTTP 500`` on five of six identical requests, body::

    {"error":"Failed to build transaction",
     "details":"Failed to fetch recent blockhash: RPC request failed: HTTP 429"}

Its own upstream RPC was rate-limiting it, for a blockhash Gecko throws away anyway
(:mod:`gecko.prepare_purchase` re-stamps a fresh one at the layout's offset). Every
stranger who tried one click that morning hit that wall.

WHAT IS HERE AND WHAT IS NOT. The encoder is NOT here — it is
:mod:`gecko.instruction_build`, which takes an IDL instruction as data and knows nothing
about this program. What is here is this program's ABI, copied verbatim from its IDL, and
the one call that hands it over. A second program's local build adds its own constant
beside this one; if it ever needs a change in ``instruction_build``, that is the engine
growing a program-shaped branch and it is the thing this split exists to prevent.

What is asserted, and where it came from. The account ORDER and the writable/signer flags
below are the IDL's (``capabilities/let_me_buy.idl.json``, matched by
``tests/fixtures/let_me_buy_idl.json``), and the discriminator is the IDL's eight bytes —
cross-checked against ``sha256("global:make_purchase")[:8]`` by
:func:`gecko.artifact.instruction_encoding`, which refuses when the two disagree. Both are
pinned by ``tests/test_let_me_buy_local_build.py`` against the bytes mainnet LANDED for
this exact instruction (signature ``4WmhBh4y…``, ``docs/mainnet-ledger.jsonl``). If the
program upgrades and the layout moves, that test is what goes red — not a stranger's
purchase.

The output is the same :class:`~gecko.simulate.BuiltTx` shape the hosted builder returned
(a legacy, unsigned, base64 transaction with a placeholder blockhash), so the rest of the
path — signature-slot normalisation, blockhash re-stamp, simulate, bind — is untouched.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..instruction_build import (
    InstructionEncodeError,
    build_unsigned_instruction,
    encode_instruction_data,
    instruction_accounts,
    make_instruction,
)
from ..simulate import BuiltTx

__all__ = [
    "LOCAL_BUILDER_ID",
    "MAKE_PURCHASE_IDL",
    "LocalBuildError",
    "build_make_purchase",
    "encode_make_purchase_args",
    "make_purchase_instruction",
]

#: What ``built_by`` says when these bytes were assembled here. Not a URL: nothing was
#: asked, and a reader of the result should not go looking for a host that was never on
#: the path.
LOCAL_BUILDER_ID = "gecko.instruction_build:make_purchase (IDL-derived, offline)"

#: A refusal to encode. Kept as this module's name for the same error the engine raises,
#: so a caller that catches one catches both — there is only ever one class here.
LocalBuildError = InstructionEncodeError

#: THE PROGRAM'S OWN WORD, copied from its IDL and trimmed to what a builder needs: the
#: discriminator, the account slots in order with their flags, and the arg layout. The PDA
#: recipes are deliberately absent — deriving the addresses is
#: :func:`gecko.prepare_purchase._plan_accounts`'s job and is already done by the time a
#: plan reaches here.
MAKE_PURCHASE_IDL: dict[str, Any] = {
    "name": "make_purchase",
    "discriminator": [193, 62, 227, 136, 105, 212, 201, 20],
    "accounts": [
        {"name": "receipts", "writable": True},
        {"name": "signer", "writable": True, "signer": True},
        # NOT a signer: the merchant does not co-sign a sale. See the config note — the
        # program has no authority guard on this instruction, which is why the plan is
        # checked against the store's own account before anything is built.
        {"name": "authority", "writable": True},
        {"name": "mint"},
        {"name": "sender_token_account", "writable": True},
        {"name": "recipient_token_account", "writable": True},
        {
            "name": "token_program",
            "address": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        },
        {"name": "system_program", "address": "11111111111111111111111111111111"},
        {
            "name": "associated_token_program",
            "address": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        },
    ],
    "args": [
        {"name": "store_name", "type": "string"},
        {"name": "product_name", "type": "string"},
        {"name": "table_number", "type": "u8"},
    ],
}


def _program_id() -> str:
    # Lazy on purpose: the program id's single source of truth is store_directory, which
    # imports prepare_purchase, which imports this module. At call time the cycle is closed.
    from ..store_directory import LET_ME_BUY_PROGRAM_ID

    return LET_ME_BUY_PROGRAM_ID


def encode_make_purchase_args(
    store_name: str, product_name: str, table_number: int
) -> bytes:
    """The instruction data: discriminator, then the args in IDL order (Borsh)."""
    return encode_instruction_data(
        MAKE_PURCHASE_IDL,
        {
            "store_name": store_name,
            "product_name": product_name,
            "table_number": table_number,
        },
    )


def make_purchase_instruction(
    accounts: Mapping[str, str], args: Mapping[str, Any]
) -> Any:
    """A solders ``Instruction`` for ``make_purchase`` from the resolved plan.

    ``accounts`` is the map :func:`gecko.prepare_purchase._plan_accounts` produces (every
    IDL account by name, as base58); ``args`` carries ``store_name``, ``product_name``
    and ``table_number``. Raises :class:`LocalBuildError` naming the first missing piece.
    """
    return make_instruction(
        MAKE_PURCHASE_IDL,
        program_id=_program_id(),
        accounts=accounts,
        args=args,
    )


def build_make_purchase(plan: Mapping[str, Any]) -> BuiltTx:
    """The :data:`~gecko.simulate.BuildCall` that replaces Orquestra ``/build``.

    Takes the same plan the hosted builder took (``accounts``, ``args``, ``feePayer``) and
    returns the same shape: one legacy, unsigned, base64 transaction. The blockhash is the
    all-zero placeholder — the caller re-stamps a fresh one from the node it simulates
    against, exactly as it did for the hosted builder's stale one.
    """
    accounts = plan.get("accounts")
    args = plan.get("args")
    fee_payer = plan.get("feePayer")
    if not isinstance(accounts, Mapping) or not isinstance(args, Mapping):
        raise LocalBuildError("plan must carry `accounts` and `args` mappings")
    if not isinstance(fee_payer, str) or not fee_payer:
        raise LocalBuildError("plan must name a `feePayer`")
    return build_unsigned_instruction(
        MAKE_PURCHASE_IDL,
        program_id=_program_id(),
        accounts=accounts,
        args=args,
        fee_payer=fee_payer,
    )


#: The IDL's account order with (is_signer, is_writable) — derived from
#: :data:`MAKE_PURCHASE_IDL` rather than re-typed, so the two cannot drift.
MAKE_PURCHASE_ACCOUNTS: tuple[tuple[str, bool, bool], ...] = tuple(
    (slot.name, slot.is_signer, slot.is_writable)
    for slot in instruction_accounts(MAKE_PURCHASE_IDL)
)

#: Anchor's instruction discriminator: the first 8 bytes of sha256("global:<name>"), and
#: the same eight the IDL declares — ``instruction_encoding`` refuses if they differ.
MAKE_PURCHASE_DISCRIMINATOR = encode_make_purchase_args("", "", 0)[:8]

__all__ += ["MAKE_PURCHASE_ACCOUNTS", "MAKE_PURCHASE_DISCRIMINATOR"]
