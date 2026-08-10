"""``evaluate_tx`` — bind a Receipt to the exact message a signer is about to sign.

Until now a Receipt attested that *some* plan simulated clean. It could not attest that
**this** transaction is that plan. That gap is the difference between "we simulated
something like this" and "we simulated exactly this," and it is the whole reason a
pre-signature check is worth anything: without it, everything between the simulation and
the signature is unverified, and that window is exactly where a swapped account or a
rewritten route would live.

Three decisions carry this module.

**Hash the MESSAGE, not the transaction.** A Solana transaction is signatures + message.
Our simulated transaction has zeroed signature slots; the signed one does not. Hashing the
whole transaction would break the instant it is signed — the binding has to survive
signing to be useful, so it covers the message, which is precisely the bytes a signature
commits to.

**Two binding strengths, named honestly.** ``simulateTransaction`` is called with
``replaceRecentBlockhash: true``, so the blockhash we sent is NOT the one the simulation
ran against. A fork simulation therefore cannot honestly attest a blockhash:

* ``structural`` — covers the header, the account keys, the instructions and their order,
  and the fee payer, with the blockhash normalised out. This is what a simulation with a
  replaced blockhash can truthfully claim, and it catches a swapped account, an added
  instruction, or a reordered route. The blockhash's position is DERIVED from the message
  layout (:func:`blockhash_offset`), never found by searching for its value — whoever
  chooses the blockhash would otherwise choose which 32 bytes get normalised away.
* ``exact`` — covers the whole message including the blockhash. Stronger, available only
  when the simulation ran against a real blockhash it did not replace, and it expires with
  that blockhash (~150 slots, roughly a minute).

Calling a structural binding "exact" would be the lie that makes the feature worthless, so
the strength travels with the verdict and a caller can demand one.

**Fail closed.** An undecodable transaction, an unknown version, or a receipt with no
binding is ``approved=False``. "We could not check" must never render as "fine" — that is
the failure a signing gate exists to prevent.

This module reads and compares. It does not sign, does not send, and holds no key.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "BindingStrength",
    "MessageVersion",
    "SigningVerdict",
    "TxDecodeError",
    "blockhash_offset",
    "evaluate_tx",
    "message_binding",
]

#: How much of the message the binding covers. Ordered weakest → strongest.
BindingStrength = Literal["structural", "exact"]

#: The two message wire formats we decode. Single source of truth for this module and its
#: tests — never redeclare it.
MessageVersion = Literal["legacy", "v0"]

_ZERO_BLOCKHASH = bytes(32)

# Bounds for the layout walk below. These are wire facts, not guesses: a transaction is
# capped at 1232 bytes on the wire, and every account reference inside a message is a u8
# index, so 256 static keys is the hard ceiling. The instruction and lookup caps are
# generous relative to what fits in 1232 bytes; they exist so a corrupt length prefix
# costs us a refusal rather than a walk through a buffer that was never there.
_MAX_MESSAGE_BYTES = 1232
_MAX_ACCOUNT_KEYS = 256
_MAX_INSTRUCTIONS = 256
_MAX_INDEX_LIST = 256
_MAX_LOOKUPS = 64


class TxDecodeError(Exception):
    """A transaction could not be decoded. Never carries the payload."""


@dataclass(frozen=True)
class SigningVerdict:
    """Whether this exact transaction is the one a Receipt attests.

    ``approved`` is the only field a gate should branch on; ``reason`` is a short,
    value-free explanation for a human reading a log.
    """

    approved: bool
    reason: str
    strength: BindingStrength | None = None
    presented: str | None = None
    attested: str | None = None


#: Bitcoin/Solana base58 alphabet. Orquestra returns transactions base58-encoded, and
#: until now the RPC decoded them for us — we never needed it locally. Fifteen lines of
#: stdlib beats a dependency for that.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: value for value, char in enumerate(_B58)}


def _b58decode(text: str) -> bytes:
    """Decode base58, preserving leading zero bytes (encoded as leading '1's)."""
    number = 0
    for char in text:
        digit = _B58_INDEX.get(char)
        if digit is None:
            raise ValueError("non-base58 character")
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    padding = len(text) - len(text.lstrip("1"))
    return b"\x00" * padding + body


def _decode(tx: str | bytes, encoding: str) -> bytes:
    """Raw transaction bytes from whatever the builder handed back."""
    if isinstance(tx, bytes):
        return tx
    try:
        if encoding == "base58":
            return _b58decode(tx)
        return base64.b64decode(tx, validate=True)
    except Exception as exc:  # noqa: BLE001 - redact: never echo the transaction
        raise TxDecodeError(f"transaction is not valid {encoding}") from exc


def _version_of(message: Any) -> MessageVersion:
    """Which wire layout this message object serializes to."""
    return "v0" if type(message).__name__.endswith("V0") else "legacy"


def _message_of(raw: bytes) -> tuple[Any, MessageVersion]:
    """The message and its version, for a legacy or a versioned transaction."""
    try:
        from solders.transaction import VersionedTransaction

        parsed = VersionedTransaction.from_bytes(raw)
        message = parsed.message
        return message, _version_of(message)
    except Exception:
        pass
    try:
        from solders.transaction import Transaction

        return Transaction.from_bytes(raw).message, "legacy"
    except Exception as exc:  # noqa: BLE001 - redact
        raise TxDecodeError("transaction decodes as neither legacy nor v0") from exc


def _compact_u16(buf: bytes, pos: int) -> tuple[int, int]:
    """Read Solana's ShortVec length prefix: up to three bytes, seven bits each.

    Non-canonical encodings (a continuation byte contributing nothing, or a value past
    65,535) are refused — a decoder that accepts two spellings of one length lets the same
    bytes mean two different messages.
    """
    value = 0
    for group in range(3):
        if pos >= len(buf):
            raise TxDecodeError("message ends inside a length prefix")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << (group * 7)
        if not byte & 0x80:
            if group and not byte:
                raise TxDecodeError("non-canonical length prefix")
            if value > 0xFFFF:
                raise TxDecodeError("length prefix out of range")
            return value, pos
    raise TxDecodeError("length prefix longer than three bytes")


def _advance(buf: bytes, pos: int, count: int) -> int:
    """Step over ``count`` declared bytes, refusing to step past the end."""
    end = pos + count
    if end > len(buf):
        raise TxDecodeError("message ends inside a section it declared")
    return end


def blockhash_offset(serialized: bytes, version: MessageVersion) -> int:
    """Byte offset of the 32-byte ``recent_blockhash`` inside a serialized message.

    The offset is DERIVED by walking the layout — optional version prefix, three header
    bytes, the compact-u16 account-key count, that many 32-byte keys — and the blockhash
    is the next 32 bytes. The rest of the message (instructions, and for v0 the
    address-table-lookup section) is walked too, not because the offset needs it but
    because a parse that does not reach exactly the end of the buffer did not understand
    the buffer, and this function must not return an offset it cannot stand behind.

    The previous implementation searched for the blockhash BY VALUE. Any 32 bytes that
    happened to equal it — an account key, a slice of instruction data — won that race,
    and the "blockhash-blind" structural digest then covered a zeroed account key and the
    real blockhash. Whoever picks the blockhash picks which bytes get normalised out, so
    the search had to go.

    Raises :class:`TxDecodeError` on anything it cannot fully account for. It never
    returns a sentinel: two unparseable messages that both mapped to the same sentinel
    would compare EQUAL in :func:`evaluate_tx` and approve. The no-raise contract of the
    signing gate lives in ``evaluate_tx``, not here.
    """
    if not serialized:
        raise TxDecodeError("empty message")
    if len(serialized) > _MAX_MESSAGE_BYTES:
        raise TxDecodeError("message is larger than a transaction may be on the wire")

    pos = 0
    # solders' ``bytes(MessageV0)`` omits the 0x80 version prefix that the same message
    # carries inside a serialized transaction. Accept either spelling, refuse a versioned
    # prefix on something decoded as legacy (there it is the signature count, and 128
    # signatures do not fit in a packet).
    if serialized[0] & 0x80:
        if version != "v0":
            raise TxDecodeError("versioned prefix on a message decoded as legacy")
        if serialized[0] & 0x7F != 0:
            raise TxDecodeError("unsupported message version")
        pos = 1

    if pos + 3 > len(serialized):
        raise TxDecodeError("message ends inside its header")
    signers = serialized[pos]
    readonly_signed = serialized[pos + 1]
    readonly_unsigned = serialized[pos + 2]
    pos += 3

    keys, pos = _compact_u16(serialized, pos)
    if keys < 1 or keys > _MAX_ACCOUNT_KEYS:
        raise TxDecodeError(
            f"message declares {keys} account keys, outside 1..{_MAX_ACCOUNT_KEYS}"
        )
    if (
        signers < 1
        or signers > keys
        or readonly_signed > signers
        or signers + readonly_unsigned > keys
    ):
        raise TxDecodeError("message header disagrees with its account key list")

    offset = _advance(serialized, pos, 32 * keys)
    pos = _advance(serialized, offset, 32)

    instructions, pos = _compact_u16(serialized, pos)
    if instructions > _MAX_INSTRUCTIONS:
        raise TxDecodeError(f"message declares {instructions} instructions")
    for _ in range(instructions):
        if pos >= len(serialized):
            raise TxDecodeError("message ends inside an instruction")
        program_index = serialized[pos]
        pos += 1
        if program_index >= keys:
            raise TxDecodeError("instruction names a program outside the account list")
        accounts, pos = _compact_u16(serialized, pos)
        if accounts > _MAX_INDEX_LIST:
            raise TxDecodeError("instruction declares too many accounts")
        pos = _advance(serialized, pos, accounts)
        data_len, pos = _compact_u16(serialized, pos)
        pos = _advance(serialized, pos, data_len)

    if version == "v0":
        lookups, pos = _compact_u16(serialized, pos)
        if lookups > _MAX_LOOKUPS:
            raise TxDecodeError("message declares too many address table lookups")
        for _ in range(lookups):
            pos = _advance(serialized, pos, 32)
            writable, pos = _compact_u16(serialized, pos)
            if writable > _MAX_INDEX_LIST:
                raise TxDecodeError("lookup declares too many writable indexes")
            pos = _advance(serialized, pos, writable)
            readonly, pos = _compact_u16(serialized, pos)
            if readonly > _MAX_INDEX_LIST:
                raise TxDecodeError("lookup declares too many readonly indexes")
            pos = _advance(serialized, pos, readonly)

    if pos != len(serialized):
        raise TxDecodeError("message has trailing bytes after its final section")
    return offset


def _normalise_blockhash(message: Any, serialized: bytes) -> bytes:
    """Zero the message's blockhash at its STRUCTURALLY derived offset.

    Fails closed: if the derived offset does not hold the message's own blockhash we
    refuse rather than zero bytes we do not understand.

    NOTE for a later reader: that equality check is a guard against a mis-stepped parser,
    NOT proof the offset is right. It passes at a wrong offset whenever those 32 bytes are
    duplicated in the message — exactly the case where ``recent_blockhash`` equals an
    account key, and trivially true for a landing bundle built with ``Hash.default()``,
    where the blockhash and much else are both zeros. Correctness comes from the
    derivation plus the pinned-offset tests in ``tests/test_txbind.py``; do not delete
    those as redundant with this line.
    """
    try:
        blockhash = bytes(message.recent_blockhash)
    except Exception as exc:  # noqa: BLE001 - redact
        raise TxDecodeError("message exposes no recent_blockhash") from exc

    offset = blockhash_offset(serialized, _version_of(message))
    if serialized[offset : offset + 32] != blockhash:
        raise TxDecodeError("derived blockhash offset does not hold the blockhash")
    return serialized[:offset] + _ZERO_BLOCKHASH + serialized[offset + 32 :]


def message_binding(
    tx: str | bytes,
    *,
    encoding: str = "base64",
    strength: BindingStrength = "structural",
) -> str:
    """The binding hash for a transaction's message.

    ``structural`` normalises the blockhash to zero first, so the digest is stable across
    the substitution ``simulateTransaction`` performs and across re-quoting the same plan.
    ``exact`` hashes the message verbatim.

    The version and the strength are folded into the digest input, so a legacy and a v0
    message with identical contents can never collide into the same binding, and a
    structural digest can never be mistaken for an exact one.
    """
    raw = _decode(tx, encoding)
    message, version = _message_of(raw)
    body = bytes(message)
    if strength == "structural":
        body = _normalise_blockhash(message, body)

    digest = hashlib.sha256()
    digest.update(strength.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(version.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


def evaluate_tx(
    tx: str | bytes,
    receipt: Any,
    *,
    encoding: str = "base64",
    require: BindingStrength = "structural",
) -> SigningVerdict:
    """Is ``tx`` the transaction ``receipt`` attests?

    ``require`` is the minimum strength the caller accepts. Asking for ``exact`` when the
    receipt carries only a structural binding is a REFUSAL, not a silent downgrade — a
    gate that accepts a weaker proof than it demanded is not a gate.

    Never raises for a malformed transaction: a signing gate that throws is a gate that
    gets wrapped in a try/except and bypassed. Every failure path is ``approved=False``.
    """
    attested = getattr(receipt, "message_binding", None)
    strength = getattr(receipt, "binding_strength", None)

    if not attested:
        return SigningVerdict(
            approved=False,
            reason="receipt carries no binding — it attests no specific transaction",
        )
    if strength not in ("structural", "exact"):
        return SigningVerdict(
            approved=False, reason="receipt binding strength is missing or unrecognised"
        )
    if require == "exact" and strength != "exact":
        return SigningVerdict(
            approved=False,
            reason="caller requires an exact binding; the receipt is structural only",
            strength=strength,
        )
    if getattr(receipt, "status", None) != "pass":
        return SigningVerdict(
            approved=False,
            reason=f"receipt did not pass (status={getattr(receipt, 'status', None)})",
            strength=strength,
        )

    try:
        presented = message_binding(tx, encoding=encoding, strength=strength)
    except TxDecodeError as exc:
        return SigningVerdict(approved=False, reason=str(exc), strength=strength)

    if presented != attested:
        return SigningVerdict(
            approved=False,
            reason="this transaction is NOT the one the receipt attests",
            strength=strength,
            presented=presented,
            attested=attested,
        )

    caveat = " (blockhash NOT covered)" if strength == "structural" else ""
    return SigningVerdict(
        approved=True,
        reason=f"binding matches the passing receipt{caveat}",
        strength=strength,
        presented=presented,
        attested=attested,
    )
