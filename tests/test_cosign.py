"""Assembling one transaction from two independent signatures.

The attack this module exists to stop is not exotic. A co-signer is asked to sign bytes
and hands bytes back; nothing about that round trip forces them to be the same bytes. A
relay that wants the buyer's authority on a different transaction can simply return one,
and its signature will be perfectly valid — for the message it chose.

So the tests that matter here are the refusals.
"""

from __future__ import annotations

import base64

import pytest

from gecko.cosign import (
    Contribution,
    CosignRefused,
    merge_signatures,
    signature_slots,
    take_signature,
)


def _parts():
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    relay, buyer = Keypair(), Keypair()
    program = Pubkey.from_string("Vote111111111111111111111111111111111111111")
    instruction = Instruction(
        program,
        b"\x01",
        [
            AccountMeta(relay.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(buyer.pubkey(), is_signer=True, is_writable=True),
        ],
    )
    message = Message.new_with_blockhash([instruction], relay.pubkey(), Hash.default())
    unsigned = base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()
    return relay, buyer, message, unsigned


def _signed_by(message, who):
    """What a co-signer hands back: the same message, its own slot filled.

    `partial_sign(keys, blockhash)` OVERWRITES the message's blockhash with the one it
    is handed, so passing Hash.default() here silently rewrote every message back to the
    default and made a substitution test unable to fail. Pass the message's own.
    """
    from solders.transaction import Transaction

    tx = Transaction.new_unsigned(message)
    tx.partial_sign([who], message.recent_blockhash)
    return base64.b64encode(bytes(tx)).decode()


# --- the slots ---------------------------------------------------------------------


def test_slot_zero_is_the_fee_payer() -> None:
    """Not a convention to negotiate: a signature in the wrong slot is a signature by
    the wrong account, and the runtime rejects it."""
    relay, buyer, _message, unsigned = _parts()
    assert signature_slots(unsigned) == (str(relay.pubkey()), str(buyer.pubkey()))


# --- the happy path ----------------------------------------------------------------


def test_two_parties_sign_in_isolation_and_the_merge_verifies() -> None:
    relay, buyer, message, unsigned = _parts()
    expect = bytes(message)

    relay_sig = take_signature(
        _signed_by(message, relay), expect_message=expect, signer=str(relay.pubkey())
    )
    buyer_sig = take_signature(
        _signed_by(message, buyer), expect_message=expect, signer=str(buyer.pubkey())
    )

    merged = merge_signatures(
        unsigned,
        [
            Contribution(str(relay.pubkey()), relay_sig),
            Contribution(str(buyer.pubkey()), buyer_sig),
        ],
    )

    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(merged))
    assert all(tx.verify_with_results())
    assert bytes(tx.message) == expect, "the merge must not alter what executes"


# --- THE refusal this module is for ------------------------------------------------


def test_a_cosigner_that_returns_a_DIFFERENT_message_is_refused() -> None:
    """The substitution. Its signature is valid — for bytes we did not author.

    Nothing downstream that checks only signatures would catch this, which is exactly
    why the check lives here and compares BYTES rather than a decoded summary.
    """
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey

    relay, buyer, message, _unsigned = _parts()
    # The SAME two signers, a DIFFERENT payload — which is exactly the shape of the
    # attack. A relay swapping in an unrelated transaction would be caught by anything;
    # one that keeps the participants and changes what they are agreeing to is not.
    program = Pubkey.from_string("Vote111111111111111111111111111111111111111")
    other = Message.new_with_blockhash(
        [
            Instruction(
                program,
                b"\x02\x02\x02",  # different data: a different action entirely
                [
                    AccountMeta(relay.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(buyer.pubkey(), is_signer=True, is_writable=True),
                ],
            )
        ],
        relay.pubkey(),
        Hash.default(),
    )
    substituted = _signed_by(other, relay)  # validly signed, wrong message

    with pytest.raises(CosignRefused) as err:
        take_signature(
            substituted, expect_message=bytes(message), signer=str(relay.pubkey())
        )
    assert err.value.code == "message-substituted"
    assert "did not author" in err.value.reason


def test_the_message_comparison_is_bytes_not_a_decoded_summary() -> None:
    """A one-byte change to the blockhash decodes to the same accounts and the same fee
    payer. Comparing our own interpretation would wave it through."""
    from solders.hash import Hash
    from solders.message import Message

    relay, buyer, message, _unsigned = _parts()
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("Vote111111111111111111111111111111111111111")
    same_instruction = Instruction(
        program,
        b"\x01",
        [
            AccountMeta(relay.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(buyer.pubkey(), is_signer=True, is_writable=True),
        ],
    )
    moved = Message.new_with_blockhash(
        [same_instruction],
        relay.pubkey(),
        Hash.from_string("11111111111111111111111111111112"),
    )
    # Identical accounts, identical instruction, identical fee payer. ONLY the blockhash
    # differs — a decoded summary would call these the same transaction.
    assert bytes(moved) != bytes(message)
    with pytest.raises(CosignRefused) as err:
        take_signature(
            _signed_by(moved, relay),
            expect_message=bytes(message),
            signer=str(relay.pubkey()),
        )
    assert err.value.code == "message-substituted"


# --- the other refusals ------------------------------------------------------------


def test_a_cosigner_that_did_not_actually_sign_is_named() -> None:
    """An empty slot is 64 zero bytes. Returning the transaction untouched is a way of
    saying no, and it must not read as a signature."""
    relay, _buyer, message, unsigned = _parts()
    with pytest.raises(CosignRefused) as err:
        take_signature(
            unsigned, expect_message=bytes(message), signer=str(relay.pubkey())
        )
    assert err.value.code == "signature-missing"


def test_an_incomplete_merge_refuses_rather_than_look_submittable() -> None:
    relay, buyer, message, unsigned = _parts()
    relay_sig = take_signature(
        _signed_by(message, relay),
        expect_message=bytes(message),
        signer=str(relay.pubkey()),
    )
    with pytest.raises(CosignRefused) as err:
        merge_signatures(unsigned, [Contribution(str(relay.pubkey()), relay_sig)])
    assert err.value.code == "slots-unfilled"
    assert str(buyer.pubkey()) in err.value.reason


def test_a_partial_merge_is_possible_but_must_be_asked_for() -> None:
    relay, _buyer, message, unsigned = _parts()
    relay_sig = take_signature(
        _signed_by(message, relay),
        expect_message=bytes(message),
        signer=str(relay.pubkey()),
    )
    out = merge_signatures(
        unsigned,
        [Contribution(str(relay.pubkey()), relay_sig)],
        require_complete=False,
    )
    assert base64.b64decode(out)


def test_a_signature_by_the_wrong_key_does_not_pass_as_well_formed() -> None:
    """Well-formed is not correct. A 64-byte value in the right slot still has to verify
    against THIS message."""
    relay, buyer, message, unsigned = _parts()
    forged = take_signature(
        _signed_by(message, relay),
        expect_message=bytes(message),
        signer=str(relay.pubkey()),
    )
    with pytest.raises(CosignRefused) as err:
        merge_signatures(
            unsigned,
            [
                Contribution(str(relay.pubkey()), forged),
                Contribution(
                    str(buyer.pubkey()), forged
                ),  # relay's sig in buyer's slot
            ],
        )
    assert err.value.code == "signature-invalid"
    assert str(buyer.pubkey()) in err.value.reason


def test_the_same_party_cannot_contribute_twice() -> None:
    relay, _buyer, message, unsigned = _parts()
    sig = take_signature(
        _signed_by(message, relay),
        expect_message=bytes(message),
        signer=str(relay.pubkey()),
    )
    with pytest.raises(CosignRefused) as err:
        merge_signatures(
            unsigned,
            [
                Contribution(str(relay.pubkey()), sig),
                Contribution(str(relay.pubkey()), sig),
            ],
            require_complete=False,
        )
    assert err.value.code == "duplicate-contribution"


def test_a_stranger_has_no_slot_to_contribute_to() -> None:
    from solders.keypair import Keypair

    relay, _buyer, message, unsigned = _parts()
    sig = take_signature(
        _signed_by(message, relay),
        expect_message=bytes(message),
        signer=str(relay.pubkey()),
    )
    with pytest.raises(CosignRefused) as err:
        merge_signatures(
            unsigned,
            [Contribution(str(Keypair().pubkey()), sig)],
            require_complete=False,
        )
    assert err.value.code == "not-a-required-signer"


def test_undecodable_bytes_refuse_by_name() -> None:
    with pytest.raises(CosignRefused) as err:
        signature_slots("not-base64-at-all!!")
    assert err.value.code == "undecodable-transaction"


# --- the builder's one-slot array under a two-signer header -------------------------


def test_a_one_slot_array_under_a_two_signer_header_is_made_consistent() -> None:
    """Measured on the fork: the builder emits this shape whenever feePayer != signer,
    and the runtime answers SanitizeFailure before any instruction runs."""
    from solders.transaction import Transaction

    from gecko.cosign import normalize_signature_slots
    from gecko.txbind import message_binding

    _relay, _buyer, message, unsigned = _parts()
    raw = base64.b64decode(unsigned)
    # The builder's shape: same message, ONE signature slot.
    broken = bytes([1]) + bytes(64) + bytes(message)
    with pytest.raises(Exception):
        Transaction.from_bytes(broken).sanitize()  # what the runtime would say

    fixed = normalize_signature_slots(broken)
    assert fixed == raw, "the repaired bytes are exactly the honest two-slot encoding"
    assert message_binding(fixed, strength="exact") == message_binding(
        raw, strength="exact"
    ), "the message, and so the binding, is untouched"
    assert normalize_signature_slots(raw) == raw, "a consistent array is returned as is"
