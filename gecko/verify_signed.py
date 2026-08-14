"""Did the signer sign the transaction we checked?

THE GAP THIS CLOSES. Gecko hands back unsigned bytes and a receipt binding them, then
somebody else signs — PayBox, Phantom's MCP, Privy's agent CLI, a wallet adapter. They all
take the same primitive (our base64 in, signed base64 out), and between those two moments
nothing of ours runs. The bytes could have been swapped for others that sign just as
cleanly, and no custody backend in the market will notice: custody protects the KEY, never
the ACTION.

WHAT IT CAN AND CANNOT DO. It cannot prevent a bad signature — nothing keyless can. It
makes one **detectable before broadcast**, which is the strongest honest claim available to
a party that holds no key. And it is deliberately signer-agnostic: our output is already
the common input of every signer above, so verifying belongs here rather than in an
integration with any one of them.

TWO FACTS, REPORTED SEPARATELY. ``binding_matches`` answers *are these the bytes that were
checked*; ``signed`` answers *has anyone actually signed them*. They must not be collapsed,
because **the binding covers the MESSAGE and a signature is not part of the message** — so
a signer that echoes the transaction back untouched binds perfectly. A single boolean would
call that a pass. It is the same hole ``scripts/privy_backend.py`` closes at the signer,
and for the same reason: only ``verified`` (both, together) means yes.

Control plane only: this reads bytes and compares a hash. No key, no network, no account,
nothing stored.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import dataclass
from typing import Any, get_args

from .txbind import (
    BindingStrength,
    TxDecodeError,
    UnresolvedLookupError,
    message_binding,
)

__all__ = [
    "VERIFY_SIGNED_TOOL",
    "VerifiedSignature",
    "verify_signed",
    "verify_signed_result",
]

#: The valid strengths, from the single source of truth rather than a second copy.
_STRENGTHS: frozenset[str] = frozenset(get_args(BindingStrength))

#: sha256 hex — the shape `message_binding` returns.
_BINDING_CHARS = 64

#: Solana's ed25519 signature width. An all-zero slot is an EMPTY slot, never a signature.
_SIGNATURE_BYTES = 64

#: The strength a caller gets when they do not choose — ONE definition, used by the
#: function AND the transport shim. Two copies of a default is one that can drift, and the
#: drift that matters here is toward `structural`, which does not cover the blockhash.
DEFAULT_STRENGTH: BindingStrength = "exact"


@dataclass(frozen=True)
class VerifiedSignature:
    """The answer, decomposed. ``verified`` is the conjunction and nothing else."""

    verified: bool
    binding_matches: bool
    signed: bool
    reason: str
    binding_strength: str

    def to_json(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "binding_matches": self.binding_matches,
            "signed": self.signed,
            "reason": self.reason,
            "binding_strength": self.binding_strength,
        }


def verify_signed(
    transaction: str,
    binding: str,
    *,
    binding_strength: str = DEFAULT_STRENGTH,
) -> VerifiedSignature:
    """Compare a signed transaction against the binding a receipt issued for it.

    ``binding_strength`` defaults to ``exact`` — the SAFE value — inverting the rule
    ``verify_handoff`` uses. There, no default is possible because ``exact`` refuses every
    fork simulation and a caller must choose knowingly. Here the subject is a transaction
    somebody is about to broadcast, so the weak answer is the one that must be asked for by
    name: ``structural`` does not cover the blockhash and would approve bytes re-stamped
    onto a different one.

    Never raises. Every failure is a refusal carrying its reason.
    """
    if binding_strength not in _STRENGTHS:
        return _no(
            f"unknown binding strength {binding_strength!r} — expected one of "
            f"{sorted(_STRENGTHS)}",
            binding_strength=binding_strength,
        )
    if not isinstance(binding, str) or not _is_binding(binding):
        # Refused BEFORE decoding: a malformed expectation cannot be compared against
        # anything, and computing a hash to discard it invites reporting one instead.
        return _no(
            "the `binding` is not a sha256 hex digest — pass the `binding` field from "
            "the receipt that attested this transaction",
            binding_strength=binding_strength,
        )
    if not isinstance(transaction, str) or not transaction.strip():
        return _no(
            "no `transaction` to check — pass the base64 the signer returned",
            binding_strength=binding_strength,
        )

    try:
        computed = message_binding(
            transaction,
            encoding="base64",
            strength=binding_strength,  # type: ignore[arg-type]
        )
    except UnresolvedLookupError:
        # A message whose account keys live in a lookup table cannot be bound without
        # resolving the table, and resolving it needs a chain read this path does not make.
        return _no(
            "this transaction uses address lookup tables, which carry no binding here — "
            "the accounts it touches cannot be read from the message alone",
            binding_strength=binding_strength,
        )
    except TxDecodeError as exc:
        return _no(
            f"the transaction could not be decoded ({exc})",
            binding_strength=binding_strength,
        )
    except Exception as exc:  # noqa: BLE001 - a verifier answers, it does not crash
        return _no(
            f"the transaction could not be read ({type(exc).__name__})",
            binding_strength=binding_strength,
        )

    # Constant-time: the comparison decides whether somebody broadcasts, and a timing
    # oracle over a digest is a cheap thing to deny.
    matches = hmac.compare_digest(computed, binding.strip().lower())
    signed = _carries_a_signature(transaction)

    if not matches:
        return VerifiedSignature(
            verified=False,
            binding_matches=False,
            signed=signed,
            reason=(
                "this transaction does not match the binding — it is NOT the transaction "
                "that was checked. Do not broadcast it."
            ),
            binding_strength=binding_strength,
        )
    if not signed:
        return VerifiedSignature(
            verified=False,
            binding_matches=True,
            signed=False,
            reason=(
                "these ARE the bytes that were checked, and they are not signed — the "
                "signature slot is empty. A binding covers the message, and a signature "
                "is not part of the message, so an untouched transaction matches "
                "perfectly. Nothing here can land."
            ),
            binding_strength=binding_strength,
        )
    return VerifiedSignature(
        verified=True,
        binding_matches=True,
        signed=True,
        reason=(
            "the signed transaction is byte-identical, over its message, to the one the "
            "receipt attested"
        ),
        binding_strength=binding_strength,
    )


def verify_signed_result(arguments: Any) -> dict[str, Any]:
    """Transport shim for the MCP surface — parse, call, format. No logic of its own."""
    args = arguments if isinstance(arguments, dict) else {}
    return verify_signed(
        transaction=args.get("transaction") or "",
        binding=args.get("binding") or "",
        binding_strength=str(args.get("binding_strength") or DEFAULT_STRENGTH),
    ).to_json()


def _no(reason: str, *, binding_strength: str) -> VerifiedSignature:
    return VerifiedSignature(
        verified=False,
        binding_matches=False,
        signed=False,
        reason=reason,
        binding_strength=binding_strength,
    )


def _is_binding(value: str) -> bool:
    text = value.strip()
    if len(text) != _BINDING_CHARS:
        return False
    try:
        bytes.fromhex(text)
    except ValueError:
        return False
    return True


def _carries_a_signature(transaction: str) -> bool:
    """Is the first signature slot non-empty?

    Deliberately NOT a signature verification: checking an ed25519 signature needs the
    signer's public key, and a substituted transaction supplies its own. The binding is
    what proves WHICH message; this proves only that somebody filled the slot. The two
    together are the claim — either alone is not.
    """
    try:
        raw = base64.b64decode(transaction, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not raw:
        return False
    count = raw[0]  # a compact-u16 below 128 is one byte; a real tx never exceeds it
    if count == 0 or count > 127:
        return False
    first = raw[1 : 1 + _SIGNATURE_BYTES]
    return len(first) == _SIGNATURE_BYTES and any(first)


VERIFY_SIGNED_TOOL: dict[str, Any] = {
    "name": "verify_signed_transaction",
    "description": (
        "Check that a SIGNED transaction is the one Gecko attested — before you "
        "broadcast it. Pass the base64 your signer returned and the `binding` from the "
        "receipt. Answers three things: whether the bytes match what was checked "
        "(`binding_matches`), whether anyone actually signed them (`signed`), and "
        "whether both hold (`verified`). Those are separate because a signature is not "
        "part of the message a binding covers, so a signer that hands the transaction "
        "back untouched matches perfectly and can never land. Holds no key, reads no "
        "chain, stores nothing — it reads the bytes you give it and compares a hash. It "
        "cannot stop a wrong signature; it makes one detectable while that is still free."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "transaction": {
                "type": "string",
                "description": "base64 of the signed transaction your signer returned",
            },
            "binding": {
                "type": "string",
                "description": "the `binding` field from the receipt that attested it",
            },
            "binding_strength": {
                "type": "string",
                "enum": sorted(_STRENGTHS),
                "description": (
                    "defaults to `exact`, which covers the blockhash too. `structural` "
                    "does not, so bytes re-stamped onto a different blockhash would pass "
                    "— ask for it only when you know why."
                ),
            },
        },
        "required": ["transaction", "binding"],
        "additionalProperties": False,
    },
}
