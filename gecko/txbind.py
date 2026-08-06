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
  instruction, or a reordered route.
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
    "SigningVerdict",
    "TxDecodeError",
    "evaluate_tx",
    "message_binding",
]

#: How much of the message the binding covers. Ordered weakest → strongest.
BindingStrength = Literal["structural", "exact"]

_ZERO_BLOCKHASH = bytes(32)


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


def _message_of(raw: bytes) -> tuple[Any, str]:
    """The message and its version, for a legacy or a versioned transaction."""
    try:
        from solders.transaction import VersionedTransaction

        parsed = VersionedTransaction.from_bytes(raw)
        message = parsed.message
        return message, "v0" if type(message).__name__.endswith("V0") else "legacy"
    except Exception:
        pass
    try:
        from solders.transaction import Transaction

        return Transaction.from_bytes(raw).message, "legacy"
    except Exception as exc:  # noqa: BLE001 - redact
        raise TxDecodeError("transaction decodes as neither legacy nor v0") from exc


def _normalise_blockhash(message: Any, serialized: bytes) -> bytes:
    """Zero the message's blockhash without hand-parsing wire offsets.

    Locating the blockhash by value keeps this correct across message layouts. If it
    cannot be found we fail closed rather than hash bytes we do not understand.
    """
    try:
        blockhash = bytes(message.recent_blockhash)
    except Exception as exc:  # noqa: BLE001 - redact
        raise TxDecodeError("message exposes no recent_blockhash") from exc

    index = serialized.find(blockhash)
    if index < 0:
        raise TxDecodeError("blockhash not found in the serialized message")
    return serialized[:index] + _ZERO_BLOCKHASH + serialized[index + 32 :]


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
