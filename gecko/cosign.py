"""Assemble one transaction that two parties signed, without trusting either of them.

A gasless purchase needs two signatures and they come from different places. The relay
signs as fee payer because it is ``account_keys[0]``; the buyer signs as the token
authority because they are the one actually spending. Neither holds the other's key, and
neither is handed the other's signature. Each is given the SAME unsigned message, signs
it in isolation, and hands back a transaction.

THE ONE THING THIS MODULE IS FOR. A co-signer is asked to sign bytes and returns bytes.
Nothing about that round trip forces the two to be the same bytes. A relay that wants the
buyer's authority on a different transaction can return one, and every downstream check
that looks only at signatures will pass it, because the signature it returns is perfectly
valid — for the message it chose.

So the rule here is absolute and it is checked on every contribution:

    THE MESSAGE MUST BE BYTE-IDENTICAL TO THE ONE WE HANDED OUT.

Not "decodes to the same accounts", not "has the same fee payer" — byte-identical. That
is the only comparison a substitution cannot survive, and it is cheap. Anything else is
refused by name before a single signature is copied.

WHY BYTE-IDENTICAL AND NOT "EQUIVALENT". `gecko.txbind` already records that two
byte-identical v0 messages can touch different accounts through a lookup table, which is
why it refuses to bind them. The inverse matters here: two messages that decode to the
same summary can be different bytes, and the difference is exactly where an attacker
lives. Comparing decoded fields is comparing our own interpretation; comparing bytes is
comparing the thing that gets executed.

WHAT THIS MODULE DOES NOT DO. It does not sign, hold a key, talk to a relay, or submit.
It takes transactions that other things produced and assembles one. Every refusal names
a code, and no refusal message carries a raw transaction body.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal, Sequence

__all__ = [
    "CosignRefused",
    "Contribution",
    "merge_signatures",
    "normalize_signature_slots",
    "signature_slots",
    "take_signature",
    "unfilled_slots",
]

#: Every way this can refuse, as a closed set. A caller branching on a string is a caller
#: that breaks silently when the prose changes.
CosignRefusal = Literal[
    "undecodable-transaction",
    "message-substituted",
    "not-a-required-signer",
    "signature-missing",
    "signature-invalid",
    "duplicate-contribution",
    "slots-unfilled",
]


class CosignRefused(Exception):
    """A co-signature could not be trusted or a merge could not be completed.

    Carries a ``code`` from the closed set above. The message names what was wrong in
    public terms and never echoes transaction bytes.
    """

    def __init__(self, code: CosignRefusal, reason: str) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Contribution:
    """One party's signature, and who produced it."""

    pubkey: str
    signature: bytes


def _decode(tx: str | bytes) -> Any:
    from solders.transaction import Transaction, VersionedTransaction

    try:
        raw = base64.b64decode(tx, validate=True) if isinstance(tx, str) else tx
    except Exception as exc:  # noqa: BLE001 - a bad encoding is an ANSWER, not a crash
        raise CosignRefused(
            "undecodable-transaction", "these bytes are not valid base64"
        ) from exc
    for kind in (Transaction, VersionedTransaction):
        try:
            return kind.from_bytes(raw)
        except Exception:  # noqa: BLE001 - try the other shape before refusing
            continue
    raise CosignRefused(
        "undecodable-transaction",
        "these bytes are neither a legacy nor a versioned transaction",
    )


def _message_bytes(transaction: Any) -> bytes:
    return bytes(transaction.message)


def signature_slots(tx: str | bytes) -> tuple[str, ...]:
    """Who must sign, in the order the signature array expects.

    The order is not a convention to be negotiated: a signature in the wrong slot is a
    signature by the wrong account, and the runtime rejects it. Slot 0 is always the fee
    payer, which is why naming a relay as fee payer changes the binding.
    """
    transaction = _decode(tx)
    message = transaction.message
    required = int(message.header.num_required_signatures)
    return tuple(str(key) for key in list(message.account_keys)[:required])


def normalize_signature_slots(raw: bytes) -> bytes:
    """The same message, with a signature array as long as its header says.

    MEASURED, not assumed (fork, 2026-09-08 and again 2026-09-16): the Orquestra builder,
    asked for ``feePayer != signer``, emits a header saying ``num_required_signatures=2``
    over a signature array with ONE slot. The runtime answers ``SanitizeFailure`` before
    a single instruction runs, and the simulation reports a revert with zero units that
    no diagnosis can explain. So the bytes were never "a transaction" in the first place.

    This repairs the ARRAY and touches nothing else: the message is re-serialised from
    the decoded one, and the message is what every binding covers, so an ``exact``
    binding taken before and after is identical. A caller that wants proof compares the
    two. Returns ``raw`` untouched when the array already matches.
    """
    from solders.message import Message
    from solders.signature import Signature
    from solders.transaction import Transaction, VersionedTransaction

    for kind in (Transaction, VersionedTransaction):
        try:
            transaction = kind.from_bytes(raw)
        except Exception:  # noqa: BLE001 - try the other shape before refusing
            continue
        message = transaction.message
        required = int(message.header.num_required_signatures)
        if len(list(transaction.signatures)) == required:
            return raw
        if isinstance(message, Message):
            return bytes(Transaction.new_unsigned(message))
        return bytes(
            VersionedTransaction.populate(message, [Signature.default()] * required)
        )
    raise CosignRefused(
        "undecodable-transaction",
        "these bytes are neither a legacy nor a versioned transaction",
    )


def unfilled_slots(tx: str | bytes) -> tuple[str, ...]:
    """Which required signers have NOT signed yet, in slot order.

    Empty means submittable, as far as signatures go. A caller that reports a
    partially-signed transaction as signed is the mistake this exists to make visible.
    """
    transaction = _decode(tx)
    slots = signature_slots(tx)
    signatures = list(transaction.signatures)
    return tuple(name for name, sig in zip(slots, signatures) if _is_empty(sig))


def take_signature(
    returned_tx: str | bytes,
    *,
    expect_message: bytes,
    signer: str,
) -> bytes:
    """Pull ONE party's signature out of the transaction they returned.

    ``expect_message`` is the serialized message we handed them. If what comes back
    carries a different one, this refuses with ``message-substituted`` and copies
    nothing — the whole point of the module.
    """
    transaction = _decode(returned_tx)
    if _message_bytes(transaction) != expect_message:
        raise CosignRefused(
            "message-substituted",
            f"{signer} returned a signature over a DIFFERENT message than the one it "
            "was given. Its signature may well be valid; it is valid for bytes we did "
            "not author and did not attest. Nothing was copied.",
        )

    slots = [str(key) for key in list(transaction.message.account_keys)][
        : int(transaction.message.header.num_required_signatures)
    ]
    if signer not in slots:
        raise CosignRefused(
            "not-a-required-signer",
            f"{signer} is not one of this transaction's {len(slots)} required signers, "
            "so it has no slot to contribute to",
        )

    signature = list(transaction.signatures)[slots.index(signer)]
    if _is_empty(signature):
        raise CosignRefused(
            "signature-missing",
            f"{signer} returned the transaction with its own slot still empty — it was "
            "asked to sign and did not",
        )
    return bytes(signature)


def _is_empty(signature: Any) -> bool:
    """A default Signature is 64 zero bytes — the runtime's "nobody signed here"."""
    return bytes(signature) == bytes(64)


def merge_signatures(
    unsigned_tx: str | bytes,
    contributions: Sequence[Contribution],
    *,
    require_complete: bool = True,
) -> str:
    """Assemble the fully-signed transaction, base64, ready to submit.

    ``unsigned_tx`` is the authority on the message: every contribution is placed into
    ITS signature array, so nothing a co-signer returned can alter what executes. That is
    the second half of the substitution defence — `take_signature` proves the bytes
    matched, and this makes the proof irrelevant by never using their copy anyway.

    ``require_complete=False`` assembles a partially-signed transaction, for a flow that
    collects the last signature elsewhere. The default refuses to hand back something
    that looks submittable and is not.
    """
    from solders.signature import Signature
    from solders.transaction import Transaction

    transaction = _decode(unsigned_tx)
    message = transaction.message
    slots = signature_slots(unsigned_tx)

    seen: set[str] = set()
    signatures = [Signature.default() for _ in slots]
    for contribution in contributions:
        if contribution.pubkey in seen:
            raise CosignRefused(
                "duplicate-contribution",
                f"{contribution.pubkey} contributed twice; which one is authoritative "
                "is not a question this module will answer by guessing",
            )
        if contribution.pubkey not in slots:
            raise CosignRefused(
                "not-a-required-signer",
                f"{contribution.pubkey} is not a required signer of this transaction",
            )
        seen.add(contribution.pubkey)
        signatures[slots.index(contribution.pubkey)] = Signature.from_bytes(
            contribution.signature
        )

    if require_complete:
        unfilled = [name for name, sig in zip(slots, signatures) if _is_empty(sig)]
        if unfilled:
            raise CosignRefused(
                "slots-unfilled",
                f"{len(unfilled)} of {len(slots)} signatures are still missing "
                f"({', '.join(unfilled)}); this transaction would be rejected on submit",
            )

    merged = Transaction.populate(message, signatures)

    # Verify against the message rather than trusting that a well-formed signature is a
    # correct one. A signature over the right message by the wrong key is well-formed.
    if require_complete:
        results = merged.verify_with_results()
        bad = [name for name, ok in zip(slots, results) if not ok]
        if bad:
            raise CosignRefused(
                "signature-invalid",
                f"{', '.join(bad)} contributed a signature that does not verify against "
                "this message — well-formed is not the same as correct",
            )
    return base64.b64encode(bytes(merged)).decode()
