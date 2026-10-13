"""x402 v2 ``exact`` on Solana, the PAYING side: read a challenge, build the payment bytes,
encode the retry header. Pure: no network, no key, no signature.

The repo's other x402 modules CHARGE (``x402.py`` validates a challenge Gecko is shown,
``x402_pay.py`` mints Gecko's own 402). This one is the first that PAYS, so its wire
format is not ours to choose. It follows the reference implementation, read offline from
``x402`` 2.9.0 (the Python package, installed with the gecko-mcp tool) on 9 Oct:

* ``x402/http/constants.py``: the v2 headers are ``PAYMENT-REQUIRED`` (the server's
  challenge), ``PAYMENT-SIGNATURE`` (our retry) and ``PAYMENT-RESPONSE`` (its settlement
  answer); each is base64 of a JSON object (``x402/http/utils.py``).
* ``x402/schemas/payments.py``: a v2 offer (``accepts[]``) is ``scheme, network, asset,
  amount, payTo, maxTimeoutSeconds, extra``; the payload is ``{x402Version: 2, payload,
  accepted, resource, extensions}`` (``x402/client_base.py``, which echoes the server's
  ``resource`` and ``extensions``).
* ``x402/mechanisms/svm/exact/client.py``: the inner payload is ``{"transaction":
  <base64 VersionedTransaction>}``, a v0 message whose FEE PAYER is the facilitator's
  ``extra.feePayer`` (signature slot 0, left empty for it) and whose instructions are
  exactly SetComputeUnitLimit(20_000), SetComputeUnitPrice(1 microlamport),
  TransferChecked(source ATA, mint, ATA of ``payTo``, owner) and a Memo (``extra.memo``
  when the server set one, else 16 random bytes hex-encoded, for uniqueness). The client
  signs slot 1 only.
* ``x402/mechanisms/svm/exact/facilitator.py``: what a facilitator refuses, which is the
  shape above: 3 to 6 instructions, compute price <= 5 lamports, the transfer's mint and
  destination ATA equal to the offer, amount >= the offer, a memo equal to ``extra.memo``
  when one was asked for, and a fee payer that it manages.

What the reference does NOT say, and so is not assumed here: which facilitator a given
server uses, whether its ``extra`` always carries ``feePayer`` (without one the offer is
refused: a payment whose fee payer is unknown cannot be built), and how a server answers
once paid. Those are read from the live challenge, never defaulted.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "COMPUTE_BUDGET_PROGRAM",
    "DEFAULT_COMPUTE_UNIT_LIMIT",
    "DEFAULT_COMPUTE_UNIT_PRICE_MICROLAMPORTS",
    "MAX_MEMO_BYTES",
    "MEMO_PROGRAM",
    "PAYMENT_REQUIRED_HEADER",
    "PAYMENT_RESPONSE_HEADER",
    "PAYMENT_SIGNATURE_HEADER",
    "SOLANA_MAINNET",
    "Offer",
    "PaymentRequired",
    "SettleAnswer",
    "X402Error",
    "build_exact_payment",
    "decode_payment_required",
    "decode_payment_response",
    "encode_payment_signature",
    "header",
    "memo_for",
    "solana_exact_offers",
]

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"
#: CAIP-2 id of Solana mainnet (genesis hash prefix), as the v2 offers name it.
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
DEFAULT_COMPUTE_UNIT_LIMIT = 20_000
DEFAULT_COMPUTE_UNIT_PRICE_MICROLAMPORTS = 1
MAX_MEMO_BYTES = 256
_X402_VERSION = 2
_TRANSFER_CHECKED = 12
_SET_CU_LIMIT = 2
_SET_CU_PRICE = 3
#: A challenge header is a few hundred bytes; anything this large is refused unread.
_MAX_HEADER_CHARS = 32_768
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class X402Error(Exception):
    """A challenge or answer that cannot be used. Names the field, never echoes the
    attacker-written value."""


@dataclass(frozen=True)
class Offer:
    """One ``accepts[]`` entry, parsed. ``raw`` is the server's own dict, echoed back
    unchanged as ``accepted`` (a server may compare it to what it offered)."""

    scheme: str
    network: str
    asset: str
    amount: int
    pay_to: str
    max_timeout_seconds: int
    fee_payer: str | None
    memo: str | None
    raw: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class PaymentRequired:
    """A v2 challenge: its offers, and the two objects a payment echoes back."""

    accepts: tuple[Offer, ...]
    resource: Mapping[str, Any] | None
    extensions: Mapping[str, Any] | None


@dataclass(frozen=True)
class SettleAnswer:
    """The server's ``PAYMENT-RESPONSE``: did it settle, and the transaction signature."""

    success: bool
    transaction: str | None
    network: str | None
    payer: str | None
    error_reason: str | None


def header(headers: Mapping[str, str], name: str) -> str | None:
    """``name`` from ``headers``, case-insensitively (HTTP/2 sends lower case)."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _b64_json(value: str, what: str) -> Mapping[str, Any]:
    if not isinstance(value, str) or not value or len(value) > _MAX_HEADER_CHARS:
        raise X402Error(f"the {what} header is missing or too large")
    try:
        decoded = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise X402Error(f"the {what} header is not base64 JSON") from None
    if not isinstance(decoded, Mapping):
        raise X402Error(f"the {what} header is not a JSON object")
    return decoded


def _offer(raw: Mapping[str, Any]) -> Offer | None:
    """One offer, or ``None`` when it is malformed (it is then simply not selectable)."""
    scheme, network = raw.get("scheme"), raw.get("network")
    asset, pay_to = raw.get("asset"), raw.get("payTo")
    if not all(isinstance(v, str) and v for v in (scheme, network, asset, pay_to)):
        return None
    try:
        amount = int(str(raw.get("amount")))
    except ValueError:
        return None
    timeout = raw.get("maxTimeoutSeconds")
    extra = raw.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    fee_payer = extra.get("feePayer")
    memo = extra.get("memo")
    return Offer(
        scheme=str(scheme),
        network=str(network),
        asset=str(asset),
        amount=amount,
        pay_to=str(pay_to),
        max_timeout_seconds=timeout if isinstance(timeout, int) else 0,
        fee_payer=fee_payer if isinstance(fee_payer, str) and fee_payer else None,
        memo=memo if isinstance(memo, str) and memo else None,
        raw=dict(raw),
    )


def decode_payment_required(value: str) -> PaymentRequired:
    """The ``PAYMENT-REQUIRED`` header, decoded. Refuses anything but x402 v2."""
    body = _b64_json(value, PAYMENT_REQUIRED_HEADER)
    if body.get("x402Version") != _X402_VERSION:
        raise X402Error("the challenge is not x402 version 2")
    accepts = body.get("accepts")
    if not isinstance(accepts, list):
        raise X402Error("the challenge carries no accepts list")
    offers = tuple(
        offer
        for offer in (_offer(a) for a in accepts if isinstance(a, Mapping))
        if offer is not None
    )
    resource = body.get("resource")
    extensions = body.get("extensions")
    return PaymentRequired(
        accepts=offers,
        resource=resource if isinstance(resource, Mapping) else None,
        extensions=extensions if isinstance(extensions, Mapping) else None,
    )


def solana_exact_offers(
    required: PaymentRequired, *, network: str, asset: str
) -> list[Offer]:
    """The offers this payer can honour: scheme ``exact``, on ``network``, in ``asset``.
    Every other offer (other chains, other tokens, other schemes) is ignored."""
    return [
        offer
        for offer in required.accepts
        if offer.scheme == "exact" and offer.network == network and offer.asset == asset
    ]


def memo_for(offer: Offer, nonce: bytes) -> bytes:
    """The memo the reference client writes: the server's ``extra.memo`` when it set one
    (the facilitator then requires exactly it), else ``nonce`` hex-encoded."""
    if offer.memo is not None:
        data = offer.memo.encode("utf-8")
        if len(data) > MAX_MEMO_BYTES:
            raise X402Error(f"extra.memo exceeds {MAX_MEMO_BYTES} bytes")
        return data
    if len(nonce) != 16:
        raise X402Error("the memo nonce must be 16 bytes")
    return binascii.hexlify(nonce)


def build_exact_payment(
    offer: Offer,
    *,
    payer: str,
    source: str,
    destination: str,
    decimals: int,
    token_program: str,
    blockhash: str,
    nonce: bytes,
) -> bytes:
    """The unsigned ``exact`` payment for ``offer``: a v0 transaction, two signature
    slots (the facilitator's, then ``payer``'s), both empty.

    ``source`` and ``destination`` are the token accounts, derived by the caller (the
    caller pins them; this function only lays them out). The amount is the offer's
    exactly.
    """
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction

    if offer.fee_payer is None:
        raise X402Error(
            "the offer names no extra.feePayer; the payment cannot be built"
        )
    for name, value in (
        ("feePayer", offer.fee_payer),
        ("payTo", offer.pay_to),
        ("asset", offer.asset),
    ):
        if not _B58.match(value):
            raise X402Error(f"the offer's {name} is not a Solana address")
    if offer.fee_payer == payer:
        raise X402Error("the offer's fee payer is the paying wallet itself")
    if offer.amount <= 0:
        raise X402Error("the offer's amount is not positive")
    budget = Pubkey.from_string(COMPUTE_BUDGET_PROGRAM)
    instructions = [
        Instruction(
            budget,
            bytes([_SET_CU_LIMIT]) + struct.pack("<I", DEFAULT_COMPUTE_UNIT_LIMIT),
            [],
        ),
        Instruction(
            budget,
            bytes([_SET_CU_PRICE])
            + struct.pack("<Q", DEFAULT_COMPUTE_UNIT_PRICE_MICROLAMPORTS),
            [],
        ),
        Instruction(
            Pubkey.from_string(token_program),
            bytes([_TRANSFER_CHECKED]) + struct.pack("<QB", offer.amount, decimals),
            [
                AccountMeta(Pubkey.from_string(source), False, True),
                AccountMeta(Pubkey.from_string(offer.asset), False, False),
                AccountMeta(Pubkey.from_string(destination), False, True),
                AccountMeta(Pubkey.from_string(payer), True, False),
            ],
        ),
        Instruction(Pubkey.from_string(MEMO_PROGRAM), memo_for(offer, nonce), []),
    ]
    message = MessageV0.try_compile(
        Pubkey.from_string(offer.fee_payer),
        instructions,
        [],
        Hash.from_string(blockhash),
    )
    required = int(message.header.num_required_signatures)
    return bytes(
        VersionedTransaction.populate(message, [Signature.default()] * required)
    )


def encode_payment_signature(
    required: PaymentRequired, offer: Offer, transaction_base64: str
) -> str:
    """The ``PAYMENT-SIGNATURE`` header value: base64 of the v2 payment payload, with
    the server's own offer, resource and extensions echoed back."""
    body: dict[str, Any] = {
        "x402Version": _X402_VERSION,
        "payload": {"transaction": transaction_base64},
        "accepted": dict(offer.raw),
    }
    if required.resource is not None:
        body["resource"] = dict(required.resource)
    if required.extensions:
        body["extensions"] = dict(required.extensions)
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def decode_payment_response(value: str) -> SettleAnswer:
    """The ``PAYMENT-RESPONSE`` header. A missing or odd field reads as ``None``."""
    body = _b64_json(value, PAYMENT_RESPONSE_HEADER)

    def text(name: str) -> str | None:
        found = body.get(name)
        return found if isinstance(found, str) and found else None

    return SettleAnswer(
        success=body.get("success") is True,
        transaction=text("transaction"),
        network=text("network"),
        payer=text("payer"),
        error_reason=text("errorReason"),
    )
