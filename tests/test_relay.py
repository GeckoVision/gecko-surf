"""Accepting a fee payer's co-signature: the relay may append, never alter.

Offline, no relay, no key that matters: every transaction here is assembled with solders
from throwaway keys, and the "relay" is a function that returns whatever shape the test
needs. The refusals are the tests; the one happy path is the Lighthouse shape Kora
actually produces (an appended, read-only assertion and a signature in slot 0).
"""

from __future__ import annotations

import base64

import pytest

from gecko.relay import (
    LIGHTHOUSE_ASSERT_ACCOUNT_INFO,
    LIGHTHOUSE_PROGRAM,
    RelayRefused,
    accept_relay_signature,
    sponsor,
)

MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
BLOCKHASH = "So11111111111111111111111111111111111111112"


def _keys():
    from solders.keypair import Keypair

    return Keypair(), Keypair()  # relay, buyer


def _purchase_ix(relay, buyer, *, data: bytes = b"\x01buy"):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        Pubkey.from_string(MEMO),
        data,
        [
            AccountMeta(buyer.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(relay.pubkey(), is_signer=True, is_writable=True),
        ],
    )


def _lighthouse_ix(
    relay,
    *,
    writable: bool = False,
    program: str = LIGHTHOUSE_PROGRAM,
    account=None,
):
    """Kora's AssertAccountInfo on the fee payer. ``account`` swaps in another subject."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    data = (
        LIGHTHOUSE_ASSERT_ACCOUNT_INFO
        + b"\x00\x00"
        + (5000).to_bytes(8, "little")
        + b"\x04"
    )
    subject = account if account is not None else relay.pubkey()
    return Instruction(
        Pubkey.from_string(program),
        data,
        [AccountMeta(subject, is_signer=False, is_writable=writable)],
    )


def _message(instructions, payer, blockhash: str = BLOCKHASH):
    from solders.hash import Hash
    from solders.message import Message

    return Message.new_with_blockhash(
        instructions, payer.pubkey(), Hash.from_string(blockhash)
    )


def _unsigned(message) -> str:
    from solders.transaction import Transaction

    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _signed_by(message, who) -> str:
    from solders.transaction import Transaction

    tx = Transaction.new_unsigned(message)
    tx.partial_sign([who], message.recent_blockhash)
    return base64.b64encode(bytes(tx)).decode()


def _kora_shape(relay, buyer, **kw):
    """What Kora returns with Lighthouse on: our instruction, then its assertion, signed."""
    original = _message([_purchase_ix(relay, buyer)], relay)
    extended = _message(
        [_purchase_ix(relay, buyer), _lighthouse_ix(relay, **kw)], relay
    )
    return _unsigned(original), _signed_by(extended, relay), extended


def kora_extend(unsigned_base64: str, relay) -> str:
    """What a Kora relay with Lighthouse on returns for ARBITRARY unsigned bytes: the same
    instructions, its balance assertion appended, its signature in slot 0.

    Rebuilt from the compiled message rather than from Instruction objects, so a test
    can hand this any transaction another module assembled.
    """
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(unsigned_base64))
    message = tx.message
    keys = list(message.account_keys)
    header = message.header
    required = int(header.num_required_signatures)
    ro_signed = int(header.num_readonly_signed_accounts)
    ro_unsigned = int(header.num_readonly_unsigned_accounts)

    def meta(index: int) -> AccountMeta:
        writable = index < required - ro_signed or (
            required <= index < len(keys) - ro_unsigned
        )
        return AccountMeta(
            keys[index], is_signer=index < required, is_writable=writable
        )

    instructions = [
        Instruction(
            keys[int(ix.program_id_index)],
            bytes(ix.data),
            [meta(int(i)) for i in bytes(ix.accounts)],
        )
        for ix in message.instructions
    ]
    instructions.append(_lighthouse_ix(relay))
    from solders.message import Message

    extended = Message.new_with_blockhash(
        instructions, Pubkey.from_string(str(keys[0])), message.recent_blockhash
    )
    return _signed_by(extended, relay)


# --- the shape Kora produces ------------------------------------------------------------


def test_an_appended_readonly_lighthouse_assertion_signed_by_the_relay_is_accepted() -> (
    None
):
    relay, buyer = _keys()
    original, returned, extended = _kora_shape(relay, buyer)

    accepted = accept_relay_signature(
        original, returned, relay_pubkey=str(relay.pubkey())
    )

    assert accepted.transaction_base64 == returned
    assert accepted.fee_payer == str(relay.pubkey())
    assert accepted.appended_programs == (LIGHTHOUSE_PROGRAM,)
    assert accepted.extended
    from solders.pubkey import Pubkey
    from solders.signature import Signature

    assert Signature.from_bytes(accepted.relay_signature).verify(
        relay.pubkey(), bytes(extended)
    )
    assert not Signature.from_bytes(accepted.relay_signature).verify(
        Pubkey.default(), bytes(extended)
    )


def test_a_relay_that_appends_nothing_is_also_accepted() -> None:
    """Lighthouse off produces byte-identity; that is legal, and reported as not extended."""
    relay, buyer = _keys()
    message = _message([_purchase_ix(relay, buyer)], relay)
    accepted = accept_relay_signature(
        _unsigned(message), _signed_by(message, relay), relay_pubkey=str(relay.pubkey())
    )
    assert accepted.appended_programs == ()
    assert not accepted.extended


# --- the refusals ------------------------------------------------------------------------


def _refuses(original, returned, relay, code: str, **kw) -> None:
    with pytest.raises(RelayRefused) as err:
        accept_relay_signature(
            original, returned, relay_pubkey=str(relay.pubkey()), **kw
        )
    assert err.value.code == code


def test_an_extension_for_a_program_that_is_not_permitted_is_refused() -> None:
    relay, buyer = _keys()
    original, returned, _ = _kora_shape(relay, buyer, program=MEMO)
    _refuses(original, returned, relay, "extension-not-permitted")


def test_byte_identity_can_be_demanded_by_permitting_nothing() -> None:
    relay, buyer = _keys()
    original, returned, _ = _kora_shape(relay, buyer)
    _refuses(
        original,
        returned,
        relay,
        "extension-not-permitted",
        permitted_extensions=frozenset(),
    )


def test_an_extension_that_introduces_a_writable_account_is_refused() -> None:
    """Even from the permitted program. The fee payer is writable because it pays, so
    the check is on accounts the extension INTRODUCED, not on the flag it asked for."""
    from solders.keypair import Keypair

    relay, buyer = _keys()
    original, returned, _ = _kora_shape(
        relay, buyer, writable=True, account=Keypair().pubkey()
    )
    _refuses(original, returned, relay, "extension-writes")


def test_an_extension_asking_for_the_payer_writable_changes_nothing_and_passes() -> (
    None
):
    relay, buyer = _keys()
    original, returned, _ = _kora_shape(relay, buyer, writable=True)
    accepted = accept_relay_signature(
        original, returned, relay_pubkey=str(relay.pubkey())
    )
    assert accepted.extended


def test_an_altered_instruction_is_refused_even_with_a_valid_signature() -> None:
    """The substitution: same signers, same payer, different action."""
    relay, buyer = _keys()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], relay))
    altered = _message(
        [_purchase_ix(relay, buyer, data=b"\x02drain"), _lighthouse_ix(relay)], relay
    )
    _refuses(original, _signed_by(altered, relay), relay, "instruction-altered")


def test_a_removed_instruction_is_refused() -> None:
    relay, buyer = _keys()
    original = _unsigned(
        _message(
            [_purchase_ix(relay, buyer), _purchase_ix(relay, buyer, data=b"\x03")],
            relay,
        )
    )
    shorter = _message([_purchase_ix(relay, buyer)], relay)
    _refuses(original, _signed_by(shorter, relay), relay, "instruction-removed")


def test_a_moved_blockhash_is_refused() -> None:
    """Kora rewrites the blockhash when the signature array is EMPTY; ours never is, and
    if it ever came back moved the receipt over the original would attest nothing."""
    relay, buyer = _keys()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], relay))
    moved = _message(
        [_purchase_ix(relay, buyer)],
        relay,
        blockhash="11111111111111111111111111111112",
    )
    _refuses(original, _signed_by(moved, relay), relay, "blockhash-changed")


def test_a_replaced_fee_payer_is_refused() -> None:
    relay, buyer = _keys()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], relay))
    swapped = _message([_purchase_ix(relay, buyer)], buyer)  # buyer now pays
    _refuses(original, _signed_by(swapped, buyer), relay, "fee-payer-changed")


def test_bytes_that_do_not_name_the_relay_as_payer_are_refused_before_anything() -> (
    None
):
    relay, buyer = _keys()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], buyer))
    _refuses(original, original, relay, "fee-payer-changed")


def test_an_added_signer_is_refused() -> None:
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    relay, buyer = _keys()
    stranger = Keypair()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], relay))
    with_stranger = _message(
        [
            _purchase_ix(relay, buyer),
            Instruction(
                Pubkey.from_string(LIGHTHOUSE_PROGRAM),
                LIGHTHOUSE_ASSERT_ACCOUNT_INFO,
                [AccountMeta(stranger.pubkey(), is_signer=True, is_writable=False)],
            ),
        ],
        relay,
    )
    _refuses(original, _signed_by(with_stranger, relay), relay, "signers-changed")


def test_a_relay_that_did_not_sign_is_refused() -> None:
    relay, buyer = _keys()
    original, _returned, extended = _kora_shape(relay, buyer)
    _refuses(original, _unsigned(extended), relay, "relay-signature-missing")


def test_a_signature_by_the_wrong_key_in_the_relay_slot_is_refused() -> None:
    """Well-formed is not correct: 64 bytes in slot 0 that verify against nobody."""
    from solders.keypair import Keypair
    from solders.transaction import Transaction

    relay, buyer = _keys()
    original, _returned, extended = _kora_shape(relay, buyer)
    # The extended message, slot 0 signed by a stranger, slot 1 empty.
    from solders.signature import Signature

    stranger_sig = Keypair().sign_message(bytes(extended))
    forged = Transaction.populate(extended, [stranger_sig, Signature.default()])
    _refuses(
        original,
        base64.b64encode(bytes(forged)).decode(),
        relay,
        "relay-signature-invalid",
    )


def test_undecodable_bytes_refuse_by_name() -> None:
    relay, buyer = _keys()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], relay))
    _refuses(original, "not base64!!", relay, "undecodable-transaction")
    with pytest.raises(RelayRefused) as err:
        accept_relay_signature(original, "", relay_pubkey=str(relay.pubkey()))
    assert err.value.code == "relay-returned-nothing"


# --- sponsor(): the one call site -------------------------------------------------------


class _Relay:
    def __init__(self, keypair, returns=None, raises: Exception | None = None):
        self._kp = keypair
        self._returns = returns
        self._raises = raises
        self.asked: list[str] = []

    @property
    def pubkey(self) -> str:
        return str(self._kp.pubkey())

    def sign_as_fee_payer(self, unsigned_transaction_base64: str) -> str:
        self.asked.append(unsigned_transaction_base64)
        if self._raises is not None:
            raise self._raises
        return self._returns


def test_sponsor_asks_once_and_accepts_the_kora_shape() -> None:
    relay, buyer = _keys()
    original, returned, _ = _kora_shape(relay, buyer)
    party = _Relay(relay, returns=returned)
    accepted = sponsor(original, party)
    assert party.asked == [original]
    assert accepted.transaction_base64 == returned


def test_a_relay_fault_is_a_refusal_carrying_only_the_exception_type() -> None:
    relay, buyer = _keys()
    original = _unsigned(_message([_purchase_ix(relay, buyer)], relay))
    party = _Relay(relay, raises=ConnectionError("x-api-key: super-secret"))
    with pytest.raises(RelayRefused) as err:
        sponsor(original, party)
    assert err.value.code == "relay-unavailable"
    assert "super-secret" not in str(err.value)
    assert "ConnectionError" in err.value.reason
