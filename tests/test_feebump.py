"""gecko/feebump.py — the verified rebuild must prove itself or refuse.

Offline by construction: every transaction here is assembled locally with solders,
because the property under test is byte-level (what survives a rebuild), not anything
a network answers.
"""

from __future__ import annotations

import base64

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction

from gecko.feebump import COMPUTE_BUDGET_PROGRAM_ID, FeebumpError, with_priority_fee


def _unsigned_b64(instructions: list[Instruction], payer_keypair: Keypair) -> str:
    message = Message.new_with_blockhash(
        instructions, payer_keypair.pubkey(), Hash.new_unique()
    )
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _program_ix(payer: Pubkey) -> Instruction:
    """A make_purchase-shaped instruction: mixed writable/readonly, signer first."""
    return Instruction(
        Pubkey.new_unique(),
        b"\xc1\x3e\xe3\x88\x69\xd4\xc9\x14" + b"payload",
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(Pubkey.new_unique(), is_signer=False, is_writable=True),
            AccountMeta(Pubkey.new_unique(), is_signer=False, is_writable=False),
        ],
    )


def test_prepends_exactly_one_price_instruction_and_preserves_the_rest() -> None:
    payer = Keypair()
    original_ix = _program_ix(payer.pubkey())
    unsigned = _unsigned_b64([original_ix], payer)

    priced = with_priority_fee(unsigned, 3_327)

    message = Transaction.from_bytes(base64.b64decode(priced)).message
    assert len(message.instructions) == 2
    keys = list(message.account_keys)
    first, second = message.instructions
    assert keys[first.program_id_index] == COMPUTE_BUDGET_PROGRAM_ID
    assert bytes(first.data) == bytes(set_compute_unit_price(3_327).data)
    # The builder's instruction survives: same program, same data, same metas resolved.
    assert keys[second.program_id_index] == original_ix.program_id
    assert bytes(second.data) == bytes(original_ix.data)
    resolved = [keys[i] for i in bytes(second.accounts)]
    assert resolved == [meta.pubkey for meta in original_ix.accounts]


def test_payer_blockhash_and_signer_set_survive() -> None:
    payer = Keypair()
    unsigned = _unsigned_b64([_program_ix(payer.pubkey())], payer)
    before = Transaction.from_bytes(base64.b64decode(unsigned)).message

    priced = with_priority_fee(unsigned, 10_000)

    after = Transaction.from_bytes(base64.b64decode(priced)).message
    assert after.account_keys[0] == before.account_keys[0]
    assert after.recent_blockhash == before.recent_blockhash
    assert after.header.num_required_signatures == before.header.num_required_signatures


def test_zero_or_negative_bid_returns_input_unchanged() -> None:
    payer = Keypair()
    unsigned = _unsigned_b64([_program_ix(payer.pubkey())], payer)
    assert with_priority_fee(unsigned, 0) == unsigned
    assert with_priority_fee(unsigned, -5) == unsigned


def test_a_builder_that_already_priced_is_left_alone() -> None:
    """The builder's own bid wins — this module never out-bids a deliberate price."""
    payer = Keypair()
    unsigned = _unsigned_b64(
        [set_compute_unit_price(99), _program_ix(payer.pubkey())], payer
    )
    assert with_priority_fee(unsigned, 1_000_000) == unsigned


def test_a_unit_limit_alone_does_not_count_as_a_price() -> None:
    """SetComputeUnitLimit (tag 2) is not a bid; the price still gets injected."""
    payer = Keypair()
    unsigned = _unsigned_b64(
        [set_compute_unit_limit(200_000), _program_ix(payer.pubkey())], payer
    )
    priced = with_priority_fee(unsigned, 500)
    message = Transaction.from_bytes(base64.b64decode(priced)).message
    assert len(message.instructions) == 3


def test_versioned_transactions_are_refused_not_mishandled() -> None:
    payer = Keypair()
    message = MessageV0.try_compile(
        payer.pubkey(), [_program_ix(payer.pubkey())], [], Hash.new_unique()
    )
    versioned = VersionedTransaction.populate(message, [])
    unsigned = base64.b64encode(bytes(versioned)).decode()
    with pytest.raises(FeebumpError, match="versioned|legacy"):
        with_priority_fee(unsigned, 1_000)


def test_garbage_bytes_are_refused() -> None:
    with pytest.raises(FeebumpError):
        with_priority_fee(base64.b64encode(b"not a transaction").decode(), 1_000)
