"""The transaction-signer seam — every test here is a REFUSAL except two.

Offline (Pattern B): messages are assembled locally, the signing backend is a light fake,
and nothing in this file signs with a real key, contacts an RPC, or broadcasts on any
network. The fake backend counts its calls, so "it refused" is asserted as *the backend
was never reached*, not merely as *an exception was raised* — a refusal that still handed
bytes to a signer is not a refusal.
"""

from __future__ import annotations

import base64
import inspect
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from typing import Any

import pytest

from gecko.credentials import (
    ChainResolver,
    CredentialError,
    CredentialRef,
    EnvBackend,
    KeyHandle,
    resolve_key_handle,
)
from gecko.handoff import SignerHandoff
from gecko.landing import assemble_unsigned_tx
from gecko.signer import (
    DEFAULT_SIGNER_PROFILE_NAME,
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    MAX_RECEIPT_AGE_SLOTS,
    SignerProfile,
    SignerRefused,
    SigningAttestation,
    SigningBackend,
    SignedTransaction,
    TransactionSigner,
)
from gecko.simulate import BuiltTx, Receipt, TokenDeltaReport, simulate
from gecko.spend_policy import (
    AllowedInstruction,
    InMemorySpendLedger,
    SpendPolicy,
    SpendPolicyGate,
    TokenCaps,
)
from gecko.txbind import message_binding

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
FOREIGN = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
REAL_BLOCKHASH = "So11111111111111111111111111111111111111112"
SLOT = 300_000_000


def _tx(payer: str = PAYER, payload: bytes = b"gecko") -> str:
    """A minimal real unsigned transaction: one memo instruction, base64."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string(MEMO_PROGRAM)
    meta = AccountMeta(
        pubkey=Pubkey.from_string(USDC), is_signer=False, is_writable=False
    )
    return assemble_unsigned_tx(
        [Instruction(program, payload, [meta])], payer, blockhash=REAL_BLOCKHASH
    ).tx


def _receipt(
    tx: str,
    *,
    strength: str = "exact",
    network: str = "mainnet",
    observed_slot: int | None = SLOT,
    binding: str | None = None,
    sol_delta: int | None = -5_000,
    sol_delta_account: str | None = PAYER,
) -> Receipt:
    return Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=5_000,
        # G7: the signer now asks the spend policy, which reads both AMOUNT fields. A
        # receipt that resolves neither is refused as ``amount-unresolvable``, so these
        # fixtures state the two facts every signable receipt has to carry: the lamports
        # that left (here, the signature fee) and a token leg that was READ — an empty
        # ``measured`` report is an OBSERVED zero, which is what a memo transaction moves.
        sol_delta=sol_delta,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
        message_binding=(
            binding if binding is not None else message_binding(tx, strength=strength)  # type: ignore[arg-type]
        ),
        binding_strength=strength,
        lookup_resolution="none",
        network=network,  # type: ignore[arg-type]
        observed_slot=observed_slot,
        token_delta=TokenDeltaReport(status="measured", movements=(), refusals=()),
        # B2: written OUT LOUD, once, in the fixture this whole file signs through. Every
        # hand-written ``simulated`` is meant to be greppable — this is one of them, and it
        # is a fixture standing in for a run that did not happen. The tests that care about
        # that distinction use :func:`_receipt_naming_no_origin` or the real engine.
        origin="simulated",
        # N1: WHOSE lamports the number above counted. The default is the fee payer, which
        # is what every honest caller tracks; the tests that exercise the check pass the
        # recipient, or ``None``.
        sol_delta_account=sol_delta_account,
    )


def _receipt_naming_no_origin(tx: str) -> Receipt:
    """The fabricator's Receipt: every field filled in, and ``origin`` never mentioned.

    Built by DROPPING the key rather than by passing ``"asserted"``, and the difference is
    the whole test. Passing the fail-closed value explicitly would still refuse if somebody
    flipped the dataclass default to ``"simulated"`` — it would be testing the branch, not
    the default. Somebody hand-building a Receipt does not write a field they have never
    heard of, so neither does this.
    """
    genuine = _receipt(tx)
    stated = {
        field.name: getattr(genuine, field.name)
        for field in dataclass_fields(Receipt)
        if field.name != "origin"
    }
    return Receipt(**stated)


#: A local address that :func:`~gecko.rpc.validate_rpc_url` accepts and that nothing ever
#: dials: every RPC on this path is the injected fake below.
OFFLINE_RPC = "http://127.0.0.1:8899"


def _simulate_offline(tx: str) -> Receipt:
    """A GENUINE Receipt — produced by :func:`simulate`, over injected transports.

    The point of routing through the real engine rather than the ``_receipt`` factory is
    that ``origin`` is written by the code under test. A helper that stamped it here would
    make the positive control agree with itself.

    ``replace_blockhash=False`` because the signer demands ``exact``, and the node's answer
    is shaped like a real ``simulateTransaction`` result: a slot in ``context``, a lamport
    leg for the tracked account, and EMPTY token-balance arrays — an observed zero, which
    is what a memo transaction moves. ``[]`` and ``null`` are different facts here; the
    second one refuses.
    """

    def rpc_call(url: str, method: str, params: Any) -> dict[str, Any]:
        if method == "getAccountInfo":
            return {"result": {"value": {"lamports": 1_000_000}}}
        return {
            "result": {
                "context": {"slot": SLOT},
                "value": {
                    "err": None,
                    "unitsConsumed": 5_000,
                    "logs": ["Program log: memo", "Program success"],
                    "accounts": [{"lamports": 995_000}],
                    "preTokenBalances": [],
                    "postTokenBalances": [],
                },
            }
        }

    receipt = simulate(
        {"instruction": "memo"},
        rpc_url=OFFLINE_RPC,
        rpc_call=rpc_call,
        build_call=lambda _plan: BuiltTx(tx=tx, encoding="base64"),
        track=[PAYER],
        network="mainnet",
        replace_blockhash=False,
    )
    # The preconditions the signer's OTHER checks need, so a failure in this helper reads
    # as a broken fixture rather than as a broken control.
    assert receipt.status == "pass"
    assert receipt.binding_strength == "exact"
    assert receipt.observed_slot == SLOT
    assert receipt.sol_delta == -5_000
    return receipt


def _spend_gate() -> SpendPolicyGate:
    """The AUTHORIZATION predicate every signer in this file is built with.

    It is deliberately PERMISSIVE about this file's one transaction — a memo — because
    the subject of these tests is the verification seam, not the caps. What it is not is
    ABSENT: absence is its own refusal (``spend-policy-not-configured``), proved in
    ``tests/test_signer_spend_precondition.py``, and building every signer here without a
    gate would make every test below pass for that reason instead of its own.
    """
    return SpendPolicyGate(
        policy=SpendPolicy(
            authorized=True,
            per_transaction_cap_lamports=10_000_000,
            hourly_cap_lamports=100_000_000,
            daily_cap_lamports=500_000_000,
            max_transactions_per_day=100,
            allowed_instructions=frozenset(
                {AllowedInstruction(program_id=MEMO_PROGRAM, discriminator=b"gecko")}
            ),
            # An address this transaction never writes: the memo tx's only writable
            # account is the fee payer, which cap 4 exempts because the signer checks it
            # more strictly. The entry exists because an EMPTY allowlist is an unauthored
            # one, and an unauthored policy refuses.
            allowed_destinations=frozenset({USDC}),
            # An authored "this agent moves no tokens" — a sentence, not a silence.
            token_caps=TokenCaps.none(),
        ),
        ledger=InMemorySpendLedger(),
    )


def _handoff(
    tx: str | None, receipt: Receipt, *, approved: bool = True
) -> SignerHandoff:
    return SignerHandoff(
        approved=approved,
        reason="verified",
        transaction_base64=tx,
        binding=receipt.message_binding,
        strength=receipt.binding_strength,
        status=receipt.status,
        revert_class=None,
        units_consumed=receipt.units_consumed,
        network_label=receipt.network_label,
        logs_tail=(),
        network=receipt.network,
    )


class _FakeBackend:
    """A signing backend that holds no key and produces no real signature.

    It records what it was asked to sign so a refusal can be asserted as *never reached*.
    ``signed`` is the input transaction bytes echoed back, which is enough for the seam:
    the seam's job is deciding whether to call this at all.
    """

    def __init__(self, pubkey: str = PAYER, swap_to: str | None = None) -> None:
        self._pubkey = pubkey
        self._swap_to = swap_to
        self.calls: list[SigningAttestation] = []

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        self.calls.append(attestation)
        if self._swap_to is not None:
            return base64.b64decode(_tx(self._swap_to))
        return unsigned_transaction


def _profile(network: str = "mainnet", **kw: Any) -> SignerProfile:
    defaults: dict[str, Any] = {
        "name": DEFAULT_SIGNER_PROFILE_NAME,
        "network": network,
        "authorized": True,
    }
    defaults.update(kw)
    return SignerProfile(**defaults)


def _signer(backend: _FakeBackend | None = None, **kw: Any) -> TransactionSigner:
    return TransactionSigner(
        backend=backend if backend is not None else _FakeBackend(),
        profile=kw.pop("profile", _profile()),
        spend_gate=kw.pop("spend_gate", _spend_gate()),
        **kw,
    )


# --- (1) the type is the parameter: a handoff, never a string ------------------


def test_sign_takes_the_handoff_object_and_offers_no_bytes_path() -> None:
    """Condition 1: no ``str`` overload, no ``bytes`` convenience path.

    A signer whose parameter is ``str`` lets a caller separate "the bytes" from "the
    approval", which is the entire window this seam exists to close.
    """
    # `eval_str` resolves the postponed annotations, so this compares CLASSES rather than
    # the strings `from __future__ import annotations` leaves behind — a string compare
    # would pass against a parameter annotated with a lookalike name.
    signature = inspect.signature(TransactionSigner.sign, eval_str=True)
    params = list(signature.parameters.values())
    assert params[0].name == "self"
    subject = params[1]
    assert subject.name == "handoff"
    assert subject.annotation is SignerHandoff
    # Every remaining parameter is keyword-only, so the subject cannot be displaced.
    assert all(p.kind is p.KEYWORD_ONLY for p in params[2:])
    # And no parameter anywhere accepts raw transaction text.
    assert not any(p.annotation in (str, bytes, "str", "bytes") for p in params[1:])


def test_signing_refusal_carries_no_bytes() -> None:
    """A refusal is an exception with a reason and a code — never a partial result.

    ``SignerRefused`` deliberately has no payload slot: catching it cannot yield bytes,
    so the ``try/except`` a caller reaches for is not a bypass here (contrast the build
    path, where an exception erases a CHECK rather than a RESULT — see the module note).
    """
    assert not hasattr(SignerRefused("x", code="none"), "transaction_base64")
    assert issubclass(SignerRefused, Exception)
    assert not issubclass(SignerRefused, (SignedTransaction,))  # type: ignore[arg-type]


# --- (6) absence of configuration is a refusal --------------------------------


def test_default_construction_signs_nothing() -> None:
    """Condition 6: no default configuration signs anything."""
    tx = _tx()
    receipt = _receipt(tx)
    with pytest.raises(SignerRefused) as excinfo:
        TransactionSigner().sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )
    assert excinfo.value.code == "not-configured"


def test_a_profile_that_was_never_authorized_refuses() -> None:
    """``authorized`` defaults to False — the inversion of ``policy.py``'s opt-in-as-no-op."""
    assert SignerProfile(name=DEFAULT_SIGNER_PROFILE_NAME).authorized is False
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    signer = TransactionSigner(backend=backend, profile=_profile(authorized=False))
    with pytest.raises(SignerRefused) as excinfo:
        signer.sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "not-authorized"
    assert backend.calls == []


def test_a_signer_with_a_backend_but_no_profile_refuses() -> None:
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    with pytest.raises(SignerRefused) as excinfo:
        TransactionSigner(backend=backend).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT
        )
    assert excinfo.value.code == "not-configured"
    assert backend.calls == []


def test_an_unknown_network_profile_refuses() -> None:
    """``unknown`` is a legal value that approves nothing — never an opt-out."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    signer = TransactionSigner(backend=backend, profile=_profile(network="unknown"))
    with pytest.raises(SignerRefused) as excinfo:
        signer.sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "handoff-not-verified"
    assert backend.calls == []


# --- the handoff itself -------------------------------------------------------


def test_an_unapproved_handoff_refuses() -> None:
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(tx, receipt, approved=False), receipt=receipt, current_slot=SLOT
        )
    assert excinfo.value.code == "handoff-not-approved"
    assert backend.calls == []


def test_an_approved_handoff_with_no_bytes_refuses() -> None:
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(None, receipt), receipt=receipt, current_slot=SLOT
        )
    assert excinfo.value.code == "handoff-carries-no-bytes"
    assert backend.calls == []


def test_bytes_that_the_receipt_does_not_attest_refuse() -> None:
    """The swapped-transaction case: an approved handoff carrying somebody else's bytes."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    other = _tx(payload=b"not the plan you simulated")
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(other, receipt), receipt=receipt, current_slot=SLOT
        )
    assert excinfo.value.code == "handoff-not-verified"
    assert backend.calls == []


# --- C9: exact is DEMANDED, and the label alone is not trusted ----------------


def test_a_structural_receipt_refuses_because_the_signer_demands_exact() -> None:
    """C9, first of two independent reasons: ``require='exact'`` at the signing site."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, strength="structural")
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "handoff-not-verified"
    assert "exact" in str(excinfo.value)
    assert backend.calls == []


def test_a_receipt_labelled_exact_but_stale_still_refuses() -> None:
    """C9, second reason: the strength label is CALLER-ASSERTED (``simulate.py:388``).

    ``exact`` is derived from the caller's own ``replace_blockhash`` flag, never proven
    against the chain, so a receipt can carry the strongest label and still attest a
    snapshot that has aged out. The slot bound is enforced independently, inside the
    signer, so neither control alone is load-bearing.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, strength="exact", observed_slot=SLOT)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(tx, receipt),
            receipt=receipt,
            current_slot=SLOT + MAX_RECEIPT_AGE_SLOTS + 1,
        )
    assert excinfo.value.code == "receipt-too-old"
    assert backend.calls == []


def test_a_receipt_with_no_slot_refuses() -> None:
    """C5: "we could not read the age" is never "the age is fine"."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, observed_slot=None)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "receipt-slot-missing"
    assert backend.calls == []


def test_a_receipt_from_the_future_refuses() -> None:
    """A negative age is a disagreement about which chain we are on, not freshness."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx, observed_slot=SLOT)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(tx, receipt), receipt=receipt, current_slot=SLOT - 1
        )
    assert excinfo.value.code == "receipt-slot-implausible"
    assert backend.calls == []


# --- (3) the signer refuses to sign for an account it does not control --------


def test_a_foreign_fee_payer_refuses() -> None:
    """Condition 3, checked INSIDE the signer — not by the caller, not by the backend."""
    backend = _FakeBackend(pubkey=FOREIGN)
    tx = _tx(payer=PAYER)
    receipt = _receipt(tx)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "fee-payer-not-controlled"
    # The refusal names neither pubkey as a value judgement, but it must name the fact.
    assert "fee payer" in str(excinfo.value)
    assert backend.calls == []


# --- N1: the lamport delta names the account it belongs to --------------------


def test_the_gate_authorizes_a_delta_measured_on_the_recipient(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """THE COUNTEREXAMPLE, pinned before the refusal that answers it.

    ``simulate`` measures ``track[0]``, the caller picks ``track``, and nothing makes
    ``track[0]`` the fee payer. Track the RECIPIENT of a transfer and ``sol_delta`` is
    POSITIVE — so ``outflow = -delta if delta < 0 else 0`` is zero, and the spend policy
    authorizes it whatever the caps say. This test asserts the gate DOES authorize it, so
    the refusal below is documented as a real hole being closed rather than a check being
    added to something that already worked.

    Nothing here signs: it asks the gate directly, which is exactly the residual the signer
    docstring states — the gate's verdict over an unqualified amount is still wrong.
    """
    del recwarn
    tx = _tx()
    drained = _receipt(tx, sol_delta=+900_000_000, sol_delta_account=FOREIGN)
    verdict = _spend_gate().authorize(tx, drained, agent_supplied_policy=None)
    assert verdict.authorized is True, (
        "the premise of N1 has changed: the gate no longer reads a positive delta as a "
        "zero outflow, and this test should be re-derived rather than deleted"
    )


def test_a_lamport_delta_measured_on_another_account_refuses() -> None:
    """N1. A number measured on somebody else's account is not this signature's outflow.

    Same receipt the gate just authorized. The signer refuses it, because it is the only
    place where the fee payer is a fact decoded from the bytes.

    MUTATION: delete the ``lamport_account != fee_payer`` branch and these bytes are signed
    with an outflow of zero.
    """
    backend = _FakeBackend()
    tx = _tx()
    drained = _receipt(tx, sol_delta=+900_000_000, sol_delta_account=FOREIGN)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, drained), receipt=drained, current_slot=SLOT)
    assert excinfo.value.code == "receipt-lamport-subject-mismatch"
    assert backend.calls == []


def test_a_receipt_that_names_no_lamport_account_refuses() -> None:
    """ "We could not tell whose lamports those were" is never "they were the payer's".

    The missing branch has its own code: a Receipt produced before this field existed, or
    by a simulation that tracked nothing, states no account — and a signer that read that
    as the payer would be exactly the convention this change replaces.

    MUTATION: make the ``is None`` branch fall through and this signs.
    """
    backend = _FakeBackend()
    tx = _tx()
    unattributed = _receipt(tx, sol_delta_account=None)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(tx, unattributed), receipt=unattributed, current_slot=SLOT
        )
    assert excinfo.value.code == "receipt-lamport-subject-missing"
    assert backend.calls == []


def test_the_receipt_names_the_account_its_lamport_delta_belongs_to() -> None:
    """THE POSITIVE CONTROL for N1, offline, through the real :func:`simulate`.

    The account is written by the engine from ``track[0]``, not by this test, and a signer
    that refused everything would fail here.

    MUTATION: stop stamping ``sol_delta_account`` in ``simulate`` and this goes red at
    ``receipt-lamport-subject-missing``.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _simulate_offline(tx)
    assert receipt.sol_delta_account == PAYER, "the engine, not this test, wrote that"

    signed = _signer(backend).sign(
        _handoff(tx, receipt), receipt=receipt, current_slot=SLOT + 1
    )
    assert len(backend.calls) == 1
    assert signed.signer_pubkey == PAYER


def test_a_mismatched_lamport_account_burns_no_velocity_budget() -> None:
    """N1's ordering: the equality is free, so it must run before the ledger writes.

    Two DISTINCT transactions, for the reason spelled out in
    :func:`test_a_hand_built_receipt_burns_no_velocity_budget` — the reservation is
    idempotent on exact bytes, so replaying one transaction would make this blind to the
    mutation it is named after.

    MUTATION: move the lamport-account check below ``self._spend_gate().authorize(...)``
    and the second half refuses at ``spend-not-authorized``.
    """
    gate = SpendPolicyGate(
        policy=replace(_spend_gate().policy, max_transactions_per_day=1),  # type: ignore[arg-type]
        ledger=InMemorySpendLedger(),
    )
    backend = _FakeBackend()

    foreign_tx = _tx(payload=b"gecko-foreign-delta")
    misattributed = _receipt(
        foreign_tx, sol_delta=+900_000_000, sol_delta_account=FOREIGN
    )
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=gate).sign(
            _handoff(foreign_tx, misattributed),
            receipt=misattributed,
            current_slot=SLOT,
        )
    assert excinfo.value.code == "receipt-lamport-subject-mismatch"
    assert backend.calls == []

    # The one transaction a day the policy allows is still there.
    genuine_tx = _tx(payload=b"gecko-attributed")
    genuine = _simulate_offline(genuine_tx)
    signed = _signer(backend, spend_gate=gate).sign(
        _handoff(genuine_tx, genuine), receipt=genuine, current_slot=SLOT
    )
    assert signed.signer_pubkey == PAYER
    assert len(backend.calls) == 1


def test_an_undecodable_transaction_refuses_naming_the_decode() -> None:
    """C5: an undecodable anything refuses, with a reason saying which one fired."""
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    # Approved handoff, garbage payload, and a receipt binding that matches nothing.
    forged = _handoff(base64.b64encode(b"not a transaction").decode(), receipt)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(forged, receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code in ("handoff-not-verified", "undecodable-transaction")
    assert backend.calls == []


# --- the backend is not trusted either ----------------------------------------


def test_a_backend_that_returns_a_different_message_refuses() -> None:
    """The signature must cover the message that was checked, not one substituted after.

    Signing is the last hop, and it is still a hop: an external signer (option D) is a
    different party, so what it hands back is re-bound before it leaves this function.
    """
    backend = _FakeBackend(pubkey=PAYER, swap_to=FOREIGN)
    tx = _tx()
    receipt = _receipt(tx)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "backend-changed-the-message"
    assert len(backend.calls) == 1  # it WAS called; its answer was rejected


def test_a_backend_that_raises_refuses_rather_than_escaping() -> None:
    """Row 12: a locked keychain or an expired lease is a refusal, never a fall-back."""

    class _Locked:
        pubkey = PAYER

        def sign_transaction(
            self, unsigned_transaction: bytes, attestation: Any
        ) -> bytes:
            raise RuntimeError("keychain is locked")

    tx = _tx()
    receipt = _receipt(tx)
    signer = TransactionSigner(
        backend=_Locked(), profile=_profile(), spend_gate=_spend_gate()
    )
    with pytest.raises(SignerRefused) as excinfo:
        signer.sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert excinfo.value.code == "backend-unavailable"
    # The backend's own message is untrusted text that may carry a path or material, so
    # the refusal names the exception TYPE and nothing else.
    assert "keychain is locked" not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)


# --- the happy path, with a FAKE backend only (C13) ---------------------------


def test_the_happy_path_reaches_the_backend_with_an_attestation() -> None:
    backend = _FakeBackend()
    tx = _tx()
    receipt = _receipt(tx)
    signed = _signer(backend).sign(
        _handoff(tx, receipt), receipt=receipt, current_slot=SLOT + 1
    )
    assert isinstance(signed, SignedTransaction)
    assert signed.signer_pubkey == PAYER
    assert signed.binding == receipt.message_binding
    assert signed.network == "mainnet"
    assert base64.b64decode(signed.signed_transaction_base64)
    # Option D: the external signer is handed the verdict, not just the bytes.
    assert len(backend.calls) == 1
    attestation = backend.calls[0]
    assert attestation.binding == receipt.message_binding
    assert attestation.strength == "exact"
    assert attestation.network == "mainnet"
    assert attestation.fee_payer == PAYER
    assert attestation.receipt_slot == SLOT


# --- (5) local-first default, option A behind a named profile, option D open ---


def test_the_default_profile_is_the_os_keychain() -> None:
    assert DEFAULT_SIGNER_PROFILE_NAME == "os-keychain"
    assert DEVELOPER_KEYPAIR_FILE_PROFILE_NAME != DEFAULT_SIGNER_PROFILE_NAME
    assert "developer" in DEVELOPER_KEYPAIR_FILE_PROFILE_NAME


def test_the_keypair_file_profile_must_be_named_explicitly() -> None:
    """Option A is reachable, never reached by omission."""
    assert SignerProfile(name=DEFAULT_SIGNER_PROFILE_NAME).name == "os-keychain"
    developer = SignerProfile(
        name=DEVELOPER_KEYPAIR_FILE_PROFILE_NAME, network="devnet", authorized=True
    )
    assert developer.name == DEVELOPER_KEYPAIR_FILE_PROFILE_NAME
    # And even naming it does not put a path in gecko/: the profile carries no location.
    assert not any(
        "path" in field or "file" in field for field in developer.__dataclass_fields__
    )


def test_an_external_signer_satisfies_the_protocol() -> None:
    """Option D — the destination — must not be foreclosed by the Protocol's shape."""

    class _Custody:
        """Holds the key elsewhere; consults our verdict before it signs."""

        pubkey = PAYER

        def sign_transaction(
            self, unsigned_transaction: bytes, attestation: SigningAttestation
        ) -> bytes:
            assert attestation.strength == "exact"
            return unsigned_transaction

    assert isinstance(_Custody(), SigningBackend)
    tx = _tx()
    receipt = _receipt(tx)
    signed = TransactionSigner(
        backend=_Custody(), profile=_profile(), spend_gate=_spend_gate()
    ).sign(_handoff(tx, receipt), receipt=receipt, current_slot=SLOT)
    assert signed.signer_pubkey == PAYER


# --- (4) + C2: which key is a reference; the value never crosses the boundary --


def test_key_handle_is_opaque() -> None:
    """C2: the resolution returns a HANDLE, never the material.

    The AST guard in ``test_no_key_material_in_gecko.py`` checks signatures; this checks
    the runtime value, which is the half a signature cannot see.
    """
    ref = CredentialRef(api="gecko-signer")
    secret = "s3cr3t-signing-material-not-a-real-key"
    monkey = EnvBackend()
    import os

    os.environ["GECKO_CRED_GECKO_SIGNER"] = secret
    try:
        handle = resolve_key_handle(
            ref, backend_name="env", resolver=ChainResolver([monkey])
        )
    finally:
        del os.environ["GECKO_CRED_GECKO_SIGNER"]

    assert isinstance(handle, KeyHandle)
    assert not isinstance(handle, (str, bytes, bytearray))
    assert handle.backend == "env"
    assert handle.ref == ref
    # The value appears in NO field and in NO repr of the thing we hand back.
    for value in vars(handle).values():
        assert secret not in str(value)
    assert secret not in repr(handle)
    assert secret not in str(handle)


def test_key_resolution_error_never_contains_the_value() -> None:
    """Condition 4: the typed error names the ref and the backend, never the secret."""
    ref = CredentialRef(api="gecko-signer-absent")
    with pytest.raises(CredentialError) as excinfo:
        resolve_key_handle(
            ref, backend_name="env", resolver=ChainResolver([EnvBackend()])
        )
    message = str(excinfo.value)
    assert "gecko-signer-absent" in message  # the ref slot is safe to name
    assert "env" in message  # so is the backend
    # And nothing that could be material: the value was never fetched, so there is nothing
    # to leak, but the assertion is what keeps that true after the next edit.
    assert "=" not in message.split(".")[0]


def test_signing_key_resolution_never_falls_back_to_a_weaker_backend() -> None:
    """§4 row 12: never fall back to a weaker key source.

    The credential chain is a FALL-THROUGH chain (keyring → command → env) and env is the
    leakiest link. That is right for an API token and wrong for a signing key: a locked
    keychain would silently promote an env var to the thing that signs. Resolution here is
    PINNED to the named backend and a miss is a refusal.
    """
    import os

    ref = CredentialRef(api="gecko-signer")
    os.environ["GECKO_CRED_GECKO_SIGNER"] = "would-have-been-used-by-a-fallthrough"
    try:
        with pytest.raises(CredentialError) as excinfo:
            resolve_key_handle(
                ref,
                backend_name="keyring",
                resolver=ChainResolver([EnvBackend()]),
            )
    finally:
        del os.environ["GECKO_CRED_GECKO_SIGNER"]
    assert "keyring" in str(excinfo.value)


# --- C1: the residual this seam does NOT close --------------------------------


def test_the_module_docstring_states_the_fabrication_residual_honestly() -> None:
    """C1(ii): the handoff type closes OMISSION, not FABRICATION — say so, in the module.

    ``SignerHandoff`` is a plain frozen dataclass with public fields and no construction
    token, so "the type is the enforcement" is true of the OMITTED verdict and false of
    the FABRICATED one. The words 'structurally cannot' may not appear unless the handoff
    is made unforgeable.
    """
    import gecko.signer as module

    doc = module.__doc__ or ""
    assert "OMISSION" in doc and "FABRICATION" in doc
    assert "structurally cannot" not in doc.lower()


def test_a_hand_built_receipt_is_refused() -> None:
    """B2. A Receipt nobody simulated cannot reach a signature.

    THIS TEST USED TO AIM AT THE HANDOFF, as a strict ``xfail``, and it was aimed at the
    wrong object. ``sign`` runs ``verify_handoff`` itself and re-derives every fact it
    needs from its own verification result plus the Receipt; the handoff contributes only
    ``approved`` and the bytes. A hand-built handoff carrying a genuine Receipt is
    therefore the production shape with one extra allocation, and refusing it was never
    possible or desirable. The freely-constructible input that MATTERS is the Receipt.

    TWO MUTATIONS TURN THIS RED. Flip ``Receipt.origin``'s default to ``"simulated"``, or
    delete the ``receipt.origin != "simulated"`` check from ``sign``. Either way this
    fabricated Receipt — a real binding over real bytes, a plausible slot, an innocent
    lamport leg — is signed. The first mutation is only visible because the fixture NAMES
    NO ORIGIN; see :func:`_receipt_naming_no_origin`.
    """
    backend = _FakeBackend()
    tx = _tx()
    fabricated = _receipt_naming_no_origin(tx)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend).sign(
            _handoff(tx, fabricated), receipt=fabricated, current_slot=SLOT
        )
    assert excinfo.value.code == "receipt-not-simulated"
    assert backend.calls == []


def test_the_default_receipt_origin_is_the_one_that_refuses() -> None:
    """The value reached by OMISSION must be the value that signs nothing.

    A Receipt built by third-party code, or by code written against the older field set,
    names no origin at all. What it gets by default decides whether that path refuses or
    signs, so the default is asserted here directly rather than only through the signer.
    """
    from dataclasses import fields as dataclass_fields

    origin_field = next(f for f in dataclass_fields(Receipt) if f.name == "origin")
    assert origin_field.default == "asserted"


def test_a_simulated_receipt_reaches_the_backend() -> None:
    """THE POSITIVE CONTROL for B2, offline, through the real :func:`simulate`.

    Without this, "it refuses a hand-built Receipt" is indistinguishable from "it refuses
    everything", which is a green suite and a dead product. Nothing here is hand-stamped:
    the Receipt's ``origin`` comes from ``simulate()`` writing it, over an injected
    ``rpc_call`` and ``build_call``. No node is contacted.

    MUTATION: remove ``origin="simulated"`` from ``simulate``'s Receipt construction and
    this goes red at ``receipt-not-simulated``.
    """
    backend = _FakeBackend()
    tx = _tx()
    receipt = _simulate_offline(tx)
    assert receipt.origin == "simulated", "the engine, not this test, wrote that word"

    signed = _signer(backend).sign(
        _handoff(tx, receipt), receipt=receipt, current_slot=SLOT + 1
    )
    assert len(backend.calls) == 1
    assert signed.signer_pubkey == PAYER


def test_a_hand_built_receipt_burns_no_velocity_budget() -> None:
    """B2's ordering: the origin check is free, so it must run before the ledger writes.

    Modelled on ``test_a_stale_receipt_is_refused_before_any_budget_is_reserved``. With one
    transaction a day allowed, a refused fabrication must leave that one still available —
    otherwise an attacker who can get nothing signed can still exhaust the day's budget by
    replaying fabricated Receipts.

    MUTATION: move the origin check below ``self._spend_gate().authorize(...)`` and the
    second half of this test refuses at ``spend-not-authorized``.

    THE TWO TRANSACTIONS ARE DIFFERENT ON PURPOSE, and this is the part that is easy to get
    wrong. Since B3 the ledger's reservation is idempotent on the exact bytes, so a version
    of this test that replayed ONE transaction would be dedupe-immune: the fabrication
    would reserve, the retry would be recognised as the same transaction, and the ordering
    mutation would sail through green. Distinct payloads (both still carrying the
    allowlisted ``gecko`` prefix, which is matched as a prefix) make the second signature
    a second charge.
    """
    gate = SpendPolicyGate(
        policy=replace(_spend_gate().policy, max_transactions_per_day=1),  # type: ignore[arg-type]
        ledger=InMemorySpendLedger(),
    )
    backend = _FakeBackend()

    forged_tx = _tx(payload=b"gecko-fabricated")
    fabricated = _receipt_naming_no_origin(forged_tx)
    with pytest.raises(SignerRefused) as excinfo:
        _signer(backend, spend_gate=gate).sign(
            _handoff(forged_tx, fabricated), receipt=fabricated, current_slot=SLOT
        )
    assert excinfo.value.code == "receipt-not-simulated"
    assert backend.calls == []

    # The one transaction a day the policy allows is still there.
    genuine_tx = _tx(payload=b"gecko-genuine")
    genuine = _simulate_offline(genuine_tx)
    signed = _signer(backend, spend_gate=gate).sign(
        _handoff(genuine_tx, genuine), receipt=genuine, current_slot=SLOT
    )
    assert signed.signer_pubkey == PAYER
    assert len(backend.calls) == 1


def test_the_signer_module_never_broadcasts() -> None:
    """C13: no signing or broadcast anywhere in this batch."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "gecko" / "signer.py").read_text()
    assert "sendTransaction" not in source
    assert "Keypair" not in source
