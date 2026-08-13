"""The transaction-signer seam — the last hop, and the one that costs money.

Gecko does not sign. This module is the shape of asking somebody who does, and the set of
reasons to refuse before asking. It holds no key, reads no key, and names no key type;
:mod:`gecko.credentials` resolves *which* key as an opaque :class:`~gecko.credentials.KeyHandle`
and the material itself never enters this process through anything here. The precedent is
``scripts/sign_and_send.py:1-5`` — "nothing in ``gecko/`` signs, holds a key, or
broadcasts, and that stays true" — and autonomous signing must not be the change that
makes that sentence false. That is a one-way door: possession or control of key material
on behalf of another party is what a custody provider IS, legally, and unwinding it
changes what Gecko is rather than how it works.

WHY THE PARAMETER IS A HANDOFF AND NOT A STRING. :meth:`TransactionSigner.sign` takes
:class:`~gecko.handoff.SignerHandoff` and there is deliberately no ``bytes`` convenience
path and no ``str`` overload. ``SignerHandoff.transaction_base64`` is ``None`` on every
refusal, so a parameter of that type keeps "the bytes" and "the approval" welded into one
object; a signer whose parameter is ``str`` lets a caller separate them, which is the
entire window between simulation and signature that this seam exists to close.

WHAT THAT DOES **NOT** MAKE TRUE — the residual, stated in the honest form already set at
:mod:`gecko.signing_gate`. Required arguments of a required type close **OMISSION**: there
is no ``transaction=...`` that quietly means "unchecked", and no default that signs. They
do not close **FABRICATION**. :class:`~gecko.handoff.SignerHandoff` is a plain frozen
dataclass with public fields, no ``__post_init__`` and no provenance token, so a caller can
hand-build one with ``approved=True`` and any bytes it likes, pair it with a hand-built
Receipt, and satisfy every check below. What would close it is a construction token minted
inside :func:`~gecko.handoff.verify_handoff` and verified here — provenance ON the verdict,
not merely the verdict's shape. That is not built. The residual is recorded as a strict
``xfail`` in ``tests/test_signer.py::test_a_hand_built_handoff_is_refused``, which will
start failing the day somebody closes it.

WHY A REFUSAL IS AN EXCEPTION HERE, AND A RETURN VALUE THERE. :mod:`gecko.handoff` warns
that an exception is not a refusal object, because a caller's ``try/except`` around
``prepare_handoff`` converts "the check did not run" into "carry on" — the exception erases
a CHECK. Here it erases a RESULT: :class:`SignerRefused` has no payload slot, so catching
it yields no bytes and no signature by construction. Every refusal below is asserted in the
tests as *the backend was never reached*, not merely as *something was raised*.

THREE THINGS THIS SEAM ENFORCES THAT NOTHING UPSTREAM CAN.

1. **``exact``, demanded twice, because the label is caller-asserted.**
   ``simulate.py:388`` derives ``exact`` from the caller's own ``replace_blockhash`` flag
   and never proves it against the chain, so a receipt can carry the strongest label and
   still attest a snapshot nobody re-checked. This module passes ``require="exact"`` to
   :func:`~gecko.handoff.verify_handoff` AND independently bounds the receipt's recorded
   slot age. Two independent reasons to refuse a stale receipt; neither alone is
   load-bearing. ``Receipt.observed_slot`` was recorded-not-enforced until now — this is
   the first thing that reads it, and a receipt without one refuses.
2. **Fee payer == the signer's own account, checked INSIDE the signer.** Signing for an
   account you do not control produces a useless signature and burns a blockhash; more to
   the point, a signer that will sign for anyone is a signer whose refusals are about the
   bytes and never about the authority. Today the human does this by hand at
   ``scripts/sign_and_send.py``; here the structure does it.
3. **The backend's answer is re-bound before it leaves.** An external signer (option D,
   the destination) is a different party, so signing is still a hop. What comes back is
   re-hashed and compared to the attested binding: a backend that returns a different
   message is refused after being called, and the caller gets nothing.

ABSENCE OF CONFIGURATION IS A REFUSAL, NOT A PERMISSION. ``policy.py:29-30`` documents the
opposite — "an unset field is a no-op" — and that is right for a governed API session and
inverted here. :class:`SignerProfile` therefore defaults ``authorized`` to ``False`` and
``network`` to ``unknown``, and :class:`TransactionSigner` constructs cleanly with nothing
configured and refuses everything. The inversion is a deliberate divergence, expressed as a
distinct type rather than by quietly changing :class:`~gecko.policy.AgentPolicy`'s meaning
under its current consumers.

THE KEY OPTIONS, AND WHICH ONE IS THE DEFAULT. The local-first default is the **OS
keychain** — OS-level access control, an unlock prompt, no plaintext file. A **keypair
file on disk** is reachable only behind the explicitly named developer profile and is never
reached by omission. The destination is an **external signer that holds the key and
consults our verdict**: :class:`SigningBackend` takes the whole transaction *and* a
:class:`SigningAttestation`, so a custody backend can apply its own policy to our verdict
rather than being handed naked bytes. Nothing in this Protocol forecloses that, and nothing
in it requires a key to exist in this process.

THE SPEND VERDICT IS A PRECONDITION, NOT A COURTESY — G7. Verification is not
authorization: a perfectly simulated transfer of everything to an attacker's address
passes every check above. Until this change, :class:`~gecko.spend_policy.SpendPolicyGate`
expressed four caps and **had no caller** — the ordering lived in one demo's docstring
("so this ordering is the caller's to keep") and in a test named after the mutation. That
is a gate a caller MAY consult, and this repo has now produced the same no-caller shape
three times (``txbind.evaluate_tx``, ``signing_gate.evaluate``, and this).

So the gate is HELD HERE and CALLED HERE, and three properties make it a precondition
rather than a parameter:

* **No verdict can arrive from outside.** :meth:`TransactionSigner.sign` has no
  ``spend_verdict=``, no ``policy=`` and no ``force=``. A signer that accepts a verdict is
  a signer whose caller mints one, which is the audited-input problem moved one hop.
* **Absence is a refusal.** An unconfigured ``spend_gate`` is
  ``spend-policy-not-configured``, exactly mirroring :meth:`_configuration`. There is no
  construction of this class that signs without an authorization answer.
* **It is asked over the RE-VERIFIED bytes** — ``verified.transaction_base64``, never
  ``handoff.transaction_base64``. Authorising the caller's copy and signing the seam's
  copy is the TOCTOU window this whole seam exists to close, and it would be invisible
  today because the two are equal.

It runs AFTER the receipt-age and fee-payer checks and IMMEDIATELY BEFORE the backend is
asked, so a transaction refused by a cheap check spends no velocity budget.

WHAT THIS MODULE IS STILL NOT RESPONSIBLE FOR, SO NOBODY READS IT AS COVERED.

* **A retry double-reserves only when it re-quotes.** A backend fault (locked keychain,
  unreachable custody API) is a refusal a caller may retry. Retrying the SAME bytes is now
  deduplicated by the spend gate, which derives an ``exact`` message binding and
  recognises the replay — founder ruling D-B permitted the one-way digest into the ledger
  that this previously said was forbidden. A retry that had to refresh its blockhash is
  different bytes and does reserve again, so the counter still over-counts there. That
  residual is kept deliberately rather than closed with a blockhash-insensitive digest,
  which would let two intentionally-identical transfers collapse into one reservation;
  see :mod:`gecko.spend_policy` for the full argument.
* **What the spend gate itself cannot see**, which is documented in its own module: an
  RPC read may not decide a signature, so destinations are message account keys rather
  than resolved token-account owners, and the velocity counter is ADVISORY because the
  file it lives in is writable by the process it bounds.
* **The build path.** ``handoff.py:246`` promises "never raises for a bad build" while
  ``handoff.py:281`` calls ``simulate`` unwrapped and ``simulate.py:258/262/270`` raise
  ``SimulateError`` — so a builder failure escapes as an exception rather than a withheld
  plan, and the ``try/except`` a caller reaches for to fix that also swallows
  ``SigningRefused``, the quarantine control. That is upstream of this seam and outside
  this change's file scope. **Until it is closed, an unattended autonomous path through
  ``prepare_handoff`` is not safe to run**; this module refusing correctly does not make it
  so.
* **Broadcast.** Nothing here sends. A signed transaction is returned to the caller.

Control plane only: no transaction, no attestation and no handle is stored or logged.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .credentials import KeyHandle
from .handoff import SignerHandoff, verify_handoff
from .networks import UNKNOWN_NETWORK, Network
from .simulate import Receipt
from .spend_policy import SpendPolicyGate, SpendVerdict
from .txbind import (
    BindingStrength,
    TxDecodeError,
    _decode,
    _message_of,
    message_binding,
)

__all__ = [
    "DEFAULT_SIGNER_PROFILE_NAME",
    "DEVELOPER_KEYPAIR_FILE_PROFILE_NAME",
    "EXTERNAL_SIGNER_PROFILE_NAME",
    "MAX_RECEIPT_AGE_SLOTS",
    "RefusalCode",
    "SignedTransaction",
    "SignerProfile",
    "SignerProfileName",
    "SignerRefused",
    "SigningAttestation",
    "SigningBackend",
    "TransactionSigner",
]

#: Where the material lives, as a NAME — never as a location. The profile says which
#: custody shape is in play; the process that holds the key knows the rest.
SignerProfileName = Literal["os-keychain", "developer-keypair-file", "external-signer"]

#: Local-first default: OS-level access control and an unlock prompt, no plaintext file.
DEFAULT_SIGNER_PROFILE_NAME: SignerProfileName = "os-keychain"
#: A keypair file on disk. Weaker than the default and reachable only by NAMING it —
#: "convenient" is how a plaintext key becomes the thing everyone actually uses.
DEVELOPER_KEYPAIR_FILE_PROFILE_NAME: SignerProfileName = "developer-keypair-file"
#: The destination: the key belongs to a party whose business is custody, and we supply a
#: verdict. Gecko cannot hold a key even by mistake.
EXTERNAL_SIGNER_PROFILE_NAME: SignerProfileName = "external-signer"

#: How stale a Receipt may be before this seam refuses it, in slots.
#:
#: Sized at a blockhash's own lifetime rather than at a comfortable number: an ``exact``
#: binding dies with its blockhash, so a receipt older than that attests a transaction
#: that can no longer land, and one just under it attests state that has already moved.
#: Re-simulating is free. This is the SECOND of the two freshness reasons — the first is
#: ``require="exact"`` — and it exists because the strength label is derived from a
#: caller's flag (``simulate.py:388``) and proves nothing about what the node did.
MAX_RECEIPT_AGE_SLOTS = 150

#: Every reason this seam declines, as a closed vocabulary. A refusal must say WHICH check
#: fired: "could not be read" and "was read and was fine" are different answers and must
#: never render as the same string.
RefusalCode = Literal[
    "not-configured",
    "not-authorized",
    "handoff-not-approved",
    "handoff-carries-no-bytes",
    "handoff-not-verified",
    "receipt-slot-missing",
    "receipt-slot-implausible",
    "receipt-too-old",
    "undecodable-transaction",
    "fee-payer-not-controlled",
    # AUTHORIZATION — the second predicate, held and called here. "Nobody authored a
    # policy" and "the policy said no" are different answers and never share a code.
    "spend-policy-not-configured",
    "spend-not-authorized",
    "backend-unavailable",
    "backend-returned-nothing",
    "backend-changed-the-message",
]


class SignerRefused(Exception):
    """This seam declined. Carries a reason and a code — never bytes, never a signature.

    There is no payload attribute on purpose: catching this cannot yield something
    signable, so the ``try/except`` a caller reaches for is not a bypass here.
    """

    def __init__(
        self,
        reason: str,
        *,
        code: RefusalCode,
        verdict: SpendVerdict | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        #: The gate's own verdict when the SPEND policy is what refused, so a caller can
        #: report the typed answer without asking the gate a second time. `None` for every
        #: other refusal — there was no spend decision to carry.
        self.verdict = verdict


@dataclass(frozen=True)
class SignerProfile:
    """The human's out-of-band decision about signing. Every default refuses.

    ``authorized`` defaults to ``False`` and ``network`` to ``unknown`` — the members that
    claim nothing. A profile that was constructed but never authored therefore signs
    nothing, which is the inversion of ``policy.py``'s opt-in-as-no-op that a signer needs.

    Carries no path and no file name: naming where the material lives is the first half of
    reading it here.
    """

    name: SignerProfileName
    #: The network a signature is HEADED FOR — the caller's fact, compared against the
    #: Receipt's own structured network and against nothing else.
    network: Network = UNKNOWN_NETWORK
    #: Explicitly authored by a human, out of band. Never inferred, never widened by an
    #: agent, and never true by default.
    authorized: bool = False
    #: WHICH key, as an opaque handle. ``None`` when the backend needs no reference from
    #: us (an external signer knows its own key).
    key: KeyHandle | None = None


@dataclass(frozen=True)
class SigningAttestation:
    """What we verified, handed to the backend alongside the bytes.

    This is what makes the external-signer option (the destination) expressible rather
    than bolted on later: a custody backend receives our verdict and can apply its own
    policy to it, instead of being handed naked bytes and asked to trust the caller.
    Values only — no payload, no key, nothing to store.
    """

    binding: str
    strength: BindingStrength
    network: Network
    #: The account that pays, decoded from the message — not from anyone's claim about it.
    fee_payer: str
    receipt_slot: int
    current_slot: int
    units_consumed: int | None
    profile: SignerProfileName


@dataclass(frozen=True)
class SignedTransaction:
    """A signed transaction, returned to the caller. Nothing here broadcasts it."""

    signed_transaction_base64: str
    signer_pubkey: str
    #: The binding the Receipt attested, re-checked against the SIGNED bytes.
    binding: str
    strength: BindingStrength
    network: Network
    #: The spend verdict THIS signature was gated on. Published so a caller does not have
    #: to ask the gate a second time to learn what it decided — a second consultation
    #: reserves the budget twice and silently halves every rolling cap.
    spend_verdict: SpendVerdict | None = None


@runtime_checkable
class SigningBackend(Protocol):
    """The party that holds the key. Implemented OUTSIDE ``gecko/``, always.

    A local implementation lives in ``scripts/`` beside ``sign_and_send.py``; a custody
    provider implements the same two members remotely. Either way the material never
    crosses into this package: the Protocol asks for a public key and a signature, and
    there is no member through which a secret could arrive.
    """

    @property
    def pubkey(self) -> str:
        """The account this backend can sign for. Compared against the fee payer."""
        ...

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        """Sign, and return the full serialized transaction.

        ``attestation`` is our verdict: a backend that enforces its own policy reads it
        and may refuse. A backend that does not may ignore it.
        """
        ...


@dataclass(frozen=True)
class TransactionSigner:
    """Decide whether a handoff may be signed, then ask a backend that holds the key.

    Constructs cleanly with nothing configured, and in that state refuses everything —
    absence of configuration is a refusal, not a permission. There is no constructor that
    yields a signer which signs by default.
    """

    #: ``None`` means nothing is configured, which means nothing is signed.
    backend: SigningBackend | None = None
    profile: SignerProfile | None = None
    max_receipt_age_slots: int = MAX_RECEIPT_AGE_SLOTS
    #: The AUTHORIZATION predicate, held by the signer so it cannot be skipped by a caller
    #: that forgot it. ``None`` is a refusal, not an unmetered pass — see
    #: :meth:`_spend_gate`. It is a field rather than a parameter of :meth:`sign` because
    #: a per-call gate is a per-call decision, and the caller does not get that decision.
    spend_gate: SpendPolicyGate | None = None

    def sign(
        self,
        handoff: SignerHandoff,
        *,
        receipt: Receipt,
        current_slot: int,
    ) -> SignedTransaction:
        """Sign ``handoff``, or raise :class:`SignerRefused` naming which check fired.

        ``handoff`` is the whole subject and is a positional of type
        :class:`~gecko.handoff.SignerHandoff`. There is no ``str`` overload and no
        ``bytes`` convenience path: the approval and the bytes stay one object.

        ``receipt`` is passed separately because the handoff does not carry the recorded
        slot, and the age bound is one of the two independent freshness reasons. It must be
        the SAME Receipt the handoff was verified against — the re-verification below is
        what makes that checkable rather than assumed.

        ``current_slot`` is the caller's live observation. It is not fetched here: a
        network round trip inside a security decision turns that decision into a timeout,
        which is the classic fail-open, so this function is total and offline and the
        caller does the I/O.

        THERE IS NO PARAMETER FOR THE SPEND VERDICT, and that is the point of G7. The
        authorization answer is produced here, from the gate this signer holds, over the
        re-verified bytes; a caller cannot supply one, cannot skip one, and cannot reach
        the backend without one.
        """
        backend, profile = self._configuration()

        if not handoff.approved:
            raise SignerRefused(
                f"the handoff was not approved upstream: {handoff.reason}",
                code="handoff-not-approved",
            )
        subject = handoff.transaction_base64
        if subject is None:
            raise SignerRefused(
                "the handoff is marked approved but carries no transaction; approval and "
                "bytes are one object and this one is inconsistent",
                code="handoff-carries-no-bytes",
            )

        # RE-VERIFY, HERE, AT EXACT. Not because the upstream verdict is doubted but
        # because the strength it was taken at is the caller's choice and this call site
        # is the one that knows a signature is next. `require` has no default anywhere on
        # this path precisely so that this line has to say the word.
        verified = verify_handoff(
            subject,
            receipt,
            require="exact",
            expected_network=profile.network,
        )
        if not verified.approved or verified.transaction_base64 is None:
            raise SignerRefused(
                f"re-verification at exact strength refused these bytes: "
                f"{verified.reason}",
                code="handoff-not-verified",
            )

        self._check_receipt_age(receipt, current_slot)
        raw, fee_payer = _decode_fee_payer(verified.transaction_base64)
        if fee_payer != backend.pubkey:
            raise SignerRefused(
                "the fee payer on these bytes is not this signer's own account; refusing "
                "to sign for an account it does not control",
                code="fee-payer-not-controlled",
            )

        attested = receipt.message_binding
        recorded_slot = receipt.observed_slot
        # Re-read rather than assert. Both facts were established above — the
        # re-verification refuses a receipt with no binding or a strength below `exact`,
        # and the age check refuses one with no slot — so this branch is unreachable
        # today. It is written as a refusal anyway because an `assert` is a crash, and a
        # crash on the signing path is the shape a caller wraps in `try/except`.
        if (
            attested is None
            or receipt.binding_strength != "exact"
            or recorded_slot is None
        ):
            raise SignerRefused(
                "the receipt lost its binding, its strength or its slot between "
                "verification and signing",
                code="handoff-not-verified",
            )
        # Bound as a literal, which is what the check above just proved. Reading
        # `receipt.binding_strength` again here would carry `str` into the attestation and
        # let a widened vocabulary through without anyone re-reading this line.
        strength: BindingStrength = "exact"
        attestation = SigningAttestation(
            binding=attested,
            strength=strength,
            network=receipt.network,
            fee_payer=fee_payer,
            receipt_slot=recorded_slot,
            current_slot=current_slot,
            units_consumed=receipt.units_consumed,
            profile=profile.name,
        )

        # AUTHORIZATION, last and immediately before the key holder is asked. Everything
        # above is cheap and side-effect free; this call RESERVES velocity budget, so a
        # transaction refused by an earlier check must never reach it.
        #
        # Written INLINE rather than behind a helper on purpose: the subject must be
        # `verified.transaction_base64` — the bytes that came back out of the
        # re-verification a few lines up — and `verified` only exists here. A helper taking
        # a `str` would let a later edit pass `handoff.transaction_base64` instead, which
        # is the TOCTOU window this seam exists to close and which no behavioural test can
        # see while the two are equal. `agent_supplied_policy` is pinned to the literal
        # None: that argument exists to be refused, and a signer that forwarded one it
        # received would have re-opened the widening path the gate closes.
        verdict = self._spend_gate().authorize(
            verified.transaction_base64,
            receipt,
            agent_supplied_policy=None,
        )
        if not verdict.authorized:
            # The gate's own code travels inside the reason: "the mint was never authored"
            # and "past the hourly bound" are different answers, and flattening them here
            # would make this seam's refusal less legible than the one it received.
            raise SignerRefused(
                f"the spend policy did not authorise these bytes [{verdict.code}]: "
                f"{verdict.reason}",
                code="spend-not-authorized",
                verdict=verdict,
            )

        signed_raw = _ask_backend(backend, raw, attestation)
        _rebind(signed_raw, attested, strength)

        return SignedTransaction(
            signed_transaction_base64=base64.b64encode(signed_raw).decode(),
            signer_pubkey=backend.pubkey,
            binding=attested,
            strength=strength,
            network=receipt.network,
            spend_verdict=verdict,
        )

    def _configuration(self) -> tuple[SigningBackend, SignerProfile]:
        """The configured backend and profile, or a refusal. Never a permissive default."""
        if self.backend is None or self.profile is None:
            raise SignerRefused(
                "no signing backend and profile are configured; absence of configuration "
                "is a refusal, not a permission",
                code="not-configured",
            )
        if not self.profile.authorized:
            raise SignerRefused(
                "this signer profile was never authorized by a human, out of band; an "
                "unset authorization is a refusal here, not a no-op",
                code="not-authorized",
            )
        return self.backend, self.profile

    def _spend_gate(self) -> SpendPolicyGate:
        """The configured gate, or a refusal. Mirrors :meth:`_configuration` exactly."""
        if self.spend_gate is None:
            raise SignerRefused(
                "no spend policy gate is configured; verification is not authorization, "
                "and absence of an authorization answer is a refusal, not a permission",
                code="spend-policy-not-configured",
            )
        return self.spend_gate

    def _check_receipt_age(self, receipt: Receipt, current_slot: int) -> None:
        """The second, independent freshness reason. A receipt does not expire on its own."""
        recorded = receipt.observed_slot
        if recorded is None:
            raise SignerRefused(
                "the receipt records no slot, so its age cannot be read; 'we could not "
                "check' is never 'it is fresh'",
                code="receipt-slot-missing",
            )
        age = current_slot - recorded
        if age < 0:
            # Not staleness — a disagreement about which chain these two observations came
            # from. Reading it as "very fresh" is how a fork receipt gets signed on
            # mainnet.
            raise SignerRefused(
                f"the receipt records a slot ahead of the current one by {-age}; these "
                f"two observations are not of the same chain",
                code="receipt-slot-implausible",
            )
        if age > self.max_receipt_age_slots:
            raise SignerRefused(
                f"the receipt is {age} slots old, past the {self.max_receipt_age_slots} "
                f"slot bound; re-simulate, it is free",
                code="receipt-too-old",
            )


def _decode_fee_payer(transaction_base64: str) -> tuple[bytes, str]:
    """The raw bytes and the fee payer decoded FROM them — never from a claim about them.

    An undecodable transaction is a refusal, not a skipped check: the account that pays is
    the one fact this seam needs about authority, and not being able to read it is the
    strongest reason of all to stop.
    """
    try:
        raw = _decode(transaction_base64, "base64")
        message, _version = _message_of(raw)
        keys = list(message.account_keys)
    except (TxDecodeError, TypeError, ValueError) as exc:
        # Redacted: the type only. The transaction is never echoed into an error.
        raise SignerRefused(
            f"the transaction could not be decoded to read its fee payer "
            f"({type(exc).__name__})",
            code="undecodable-transaction",
        ) from None
    if not keys:
        raise SignerRefused(
            "the transaction carries no account keys, so it names no fee payer",
            code="undecodable-transaction",
        )
    return raw, str(keys[0])


def _ask_backend(
    backend: SigningBackend, raw: bytes, attestation: SigningAttestation
) -> bytes:
    """Call the backend, converting any fault into a refusal — never a fall-back.

    A locked keychain, an expired lease and an unreachable custody API are the same answer
    here: no signature. The one thing this may never do is reach for a weaker key source,
    which is exactly what the credential chain would do on its own and why
    :func:`~gecko.credentials.resolve_key_handle` pins the backend instead.
    """
    try:
        signed = backend.sign_transaction(raw, attestation)
    except SignerRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - any backend fault is a refusal
        # The type name only: a backend's exception message is untrusted text that may
        # carry a path, a prompt, or material.
        raise SignerRefused(
            f"the signing backend did not produce a signature ({type(exc).__name__}); "
            f"a signer never falls back to a weaker key source",
            code="backend-unavailable",
        ) from None
    if not signed:
        raise SignerRefused(
            "the signing backend returned no transaction",
            code="backend-returned-nothing",
        )
    return signed


def _rebind(signed_raw: bytes, attested: str, strength: BindingStrength) -> None:
    """Does the SIGNED transaction still carry the message that was checked?

    Signing is the last hop and it is still a hop. A binding is taken over the message, and
    a signature lives outside the message, so this digest is unchanged by signing and
    changed by anything else — which is the only property that makes the comparison worth
    making.
    """
    try:
        rebound = message_binding(signed_raw, strength=strength)
    except TxDecodeError as exc:
        raise SignerRefused(
            f"the signed transaction could not be re-bound ({type(exc).__name__})",
            code="backend-changed-the-message",
        ) from None
    if rebound != attested:
        raise SignerRefused(
            "the signed transaction does not carry the message that was verified",
            code="backend-changed-the-message",
        )
