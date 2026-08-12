"""The declared default signer profile now has a backend — N2.

``DEFAULT_SIGNER_PROFILE_NAME`` has been ``"os-keychain"`` since the seam was written and
nothing implemented it, so the documented-secure default refused every transaction while
the two WEAKER profiles were the only ones that worked. These tests hold the closure of
that gap, and — more importantly — hold the properties that make it worth closing this way
rather than by putting a key on disk.

Everything here runs against a FAKE keyring. No test touches a real keychain, so the suite
is the same on a developer laptop, on a headless box with no Secret Service, and in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.transaction import Transaction

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gecko.credentials import CredentialRef, _KEYRING_USER, _service
from gecko.networks import network_from_label
from gecko.signer import (
    DEFAULT_SIGNER_PROFILE_NAME,
    SigningAttestation,
    SigningBackend,
)
from os_keychain_backend import (  # noqa: E402
    KEYRING_BACKEND_NAME,
    KeychainSignerError,
    OsKeychainBackend,
    _decode_secret,
)

REF = CredentialRef(api="solana-signer")


class FakeKeyring:
    """The keyring library's surface, as much of it as this backend uses.

    ``get_keyring`` is what ``KeyringBackend.available()`` probes; returning a plain object
    reads as "a real encrypted store" because ``_is_null_or_fail`` matches only the
    ``.fail``/``.null`` backend modules. Same shape as ``tests/test_credentials.py``'s fake.
    """

    def __init__(self, entries: dict[tuple[str, str], str] | None = None) -> None:
        self.entries = entries or {}
        self.reads = 0

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service: str, user: str) -> str | None:
        self.reads += 1
        return self.entries.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.entries[(service, user)] = value


def _store(keypair: Keypair, *, as_base58: bool = False) -> FakeKeyring:
    material = str(keypair) if as_base58 else json.dumps(list(bytes(keypair)))
    ring = FakeKeyring()
    ring.set_password(_service(REF.slot()), _KEYRING_USER, material)
    return ring


def _unsigned(fee_payer: Keypair) -> bytes:
    """An unsigned transaction whose fee payer is ``fee_payer``, with no instructions."""
    message = Message.new_with_blockhash([], fee_payer.pubkey(), Hash.default())
    return bytes(Transaction.new_unsigned(message))


def _attestation(fee_payer: str) -> SigningAttestation:
    return SigningAttestation(
        binding="b" * 64,
        strength="exact",
        network=network_from_label("mainnet"),
        fee_payer=fee_payer,
        receipt_slot=100,
        current_slot=101,
        units_consumed=1_000,
        profile=DEFAULT_SIGNER_PROFILE_NAME,
    )


# --- the gap this closes ---------------------------------------------------------


def test_the_declared_default_profile_now_has_a_backend() -> None:
    """N2. The point of the whole file: the default is the thing that works."""
    keypair = Keypair()
    backend = OsKeychainBackend.open(REF, keyring_module=_store(keypair))

    assert backend.pubkey == str(keypair.pubkey())
    profile = backend.profile(network="mainnet")
    assert profile.name == DEFAULT_SIGNER_PROFILE_NAME


def test_it_satisfies_the_signing_backend_protocol_structurally() -> None:
    """Not "has the right method names" — checked against the Protocol itself.

    ``SigningBackend`` is ``runtime_checkable``, so this is the same question
    ``TransactionSigner`` asks. A backend that merely looked right would be discovered at
    the moment of signing, which is the worst moment to discover it.
    """
    backend = OsKeychainBackend.open(REF, keyring_module=_store(Keypair()))
    assert isinstance(backend, SigningBackend)


def test_a_profile_is_never_authorized_by_constructing_one() -> None:
    """The human's decision stays the human's. ``authorized`` is False unless passed.

    This is the inversion the signer relies on: a profile that was built but never authored
    signs nothing, so the convenience method that makes the default constructible must not
    be the thing that also blesses it.
    """
    backend = OsKeychainBackend.open(REF, keyring_module=_store(Keypair()))
    assert backend.profile(network="mainnet").authorized is False
    assert backend.profile(network="mainnet", authorized=True).authorized is True


# --- the material does not live here --------------------------------------------


def test_no_attribute_on_the_instance_holds_the_secret() -> None:
    """The reason this is not just ``LocalKeypairBackend`` pointed at a keychain.

    A keychain's premise is that the secret is at rest and access is an event. Holding a
    Keypair for the process lifetime would discard that premise while keeping the prompt.
    Checked by scanning every attribute for the secret in BOTH stored encodings, rather
    than by asserting a particular attribute is absent — the latter passes as soon as
    someone adds a differently-named one.
    """
    keypair = Keypair()
    ring = _store(keypair)
    backend = OsKeychainBackend.open(REF, keyring_module=ring)

    secret_bytes = bytes(keypair)
    forbidden = {json.dumps(list(secret_bytes)), str(keypair), secret_bytes.hex()}

    for name, value in vars(backend).items():
        if name == "keyring_module":
            continue  # the fake ring IS the store; it is not the backend's memory
        rendered = repr(value)
        for secret in forbidden:
            assert secret not in rendered, f"{name} carries the signing key"
    assert repr(backend.handle) and "solana-signer" in repr(backend.handle)


def test_the_secret_is_re_read_for_every_signature() -> None:
    """An unlock event per signature, not one per process — observable as read counts."""
    keypair = Keypair()
    ring = _store(keypair)
    backend = OsKeychainBackend.open(REF, keyring_module=ring)

    after_open = ring.reads
    unsigned = _unsigned(keypair)
    attestation = _attestation(str(keypair.pubkey()))

    backend.sign_transaction(unsigned, attestation)
    assert ring.reads == after_open + 1
    backend.sign_transaction(unsigned, attestation)
    assert ring.reads == after_open + 2


def test_a_key_removed_after_open_is_a_refusal_not_a_stale_signature() -> None:
    """The cost of not caching, and the benefit: revocation takes effect immediately."""
    keypair = Keypair()
    ring = _store(keypair)
    backend = OsKeychainBackend.open(REF, keyring_module=ring)
    ring.entries.clear()

    with pytest.raises(KeychainSignerError) as excinfo:
        backend.sign_transaction(
            _unsigned(keypair), _attestation(str(keypair.pubkey()))
        )
    assert "no longer in the keychain" in str(excinfo.value)


# --- it signs, and it signs the right thing -------------------------------------


def test_it_produces_a_signature_that_verifies_over_the_message() -> None:
    keypair = Keypair()
    backend = OsKeychainBackend.open(REF, keyring_module=_store(keypair))
    unsigned = _unsigned(keypair)

    signed_bytes = backend.sign_transaction(
        unsigned, _attestation(str(keypair.pubkey()))
    )
    signed = Transaction.from_bytes(signed_bytes)

    signed.verify()  # raises if the signature does not match the message
    assert bytes(signed.message) == bytes(Transaction.from_bytes(unsigned).message)


def test_it_does_not_change_the_message_it_was_given() -> None:
    """``backend-changed-the-message`` is a refusal code the signer already has.

    A backend that re-serializes through a different code path can silently reorder an
    account or drop a lookup table, and the resulting signature is over bytes nobody
    simulated. Asserted here so this backend is not the one that earns that code.
    """
    keypair = Keypair()
    backend = OsKeychainBackend.open(REF, keyring_module=_store(keypair))
    unsigned = _unsigned(keypair)

    signed = Transaction.from_bytes(
        backend.sign_transaction(unsigned, _attestation(str(keypair.pubkey())))
    )
    assert bytes(signed.message) == bytes(Transaction.from_bytes(unsigned).message)


def test_it_refuses_when_the_attestation_names_a_different_fee_payer() -> None:
    """Signing for an account this key is not produces a useless signature.

    The signer checks the same fact against the DECODED message, so this is a second
    reading of one fact rather than a substitute for it — and a disagreement means the
    attestation does not describe these bytes.
    """
    keypair = Keypair()
    backend = OsKeychainBackend.open(REF, keyring_module=_store(keypair))

    with pytest.raises(KeychainSignerError) as excinfo:
        backend.sign_transaction(
            _unsigned(keypair), _attestation(str(Keypair().pubkey()))
        )
    assert "not the same account" in str(excinfo.value)


# --- fail closed, and fail early ------------------------------------------------


def test_opening_with_no_key_in_the_keychain_refuses() -> None:
    """A backend that cannot reach its key says so at CONFIGURATION time.

    Not at the moment a caller holds a verified transaction and every other check has
    already passed — that is the moment where a refusal reads as an outage and invites
    someone to reach for the plaintext-file profile.
    """
    with pytest.raises(KeychainSignerError) as excinfo:
        OsKeychainBackend.open(REF, keyring_module=FakeKeyring())
    message = str(excinfo.value)
    assert "solana-signer" in message
    assert KEYRING_BACKEND_NAME in message


def test_a_backend_built_without_open_refuses_to_report_a_pubkey() -> None:
    """Bypassing ``open()`` bypasses the reachability proof, so it may not look ready."""
    with pytest.raises(KeychainSignerError):
        _ = OsKeychainBackend(ref=REF, keyring_module=FakeKeyring()).pubkey


def test_resolution_is_pinned_to_the_keychain_and_never_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked keychain must not promote an environment variable to the signer.

    The credential CHAIN is right for an API token, where a miss costs a 401. Here a
    fall-through would mean the leakiest link becomes the thing that signs, by accident, at
    the one moment nobody wanted a downgrade. So: key in env, nothing in the keychain,
    still a refusal.
    """
    keypair = Keypair()
    monkeypatch.setenv("GECKO_CRED_SOLANA_SIGNER", json.dumps(list(bytes(keypair))))

    with pytest.raises(KeychainSignerError):
        OsKeychainBackend.open(REF, keyring_module=FakeKeyring())


# --- the stored formats ---------------------------------------------------------


def test_both_stored_formats_yield_the_same_key() -> None:
    """A JSON byte array is what solana-keygen writes; base58 is what a wallet exports.

    Both are supported so moving a key OFF disk and into the keychain is not also a format
    migration — friction there is friction pushing someone back toward the file profile.
    """
    keypair = Keypair()
    from_json = OsKeychainBackend.open(REF, keyring_module=_store(keypair))
    from_base58 = OsKeychainBackend.open(
        REF, keyring_module=_store(keypair, as_base58=True)
    )
    assert from_json.pubkey == from_base58.pubkey == str(keypair.pubkey())


@pytest.mark.parametrize(
    "material",
    [
        "",
        "   ",
        "not-base58-at-all-!!!",
        "[1, 2, 3]",  # right shape, wrong length
        "[]",
    ],
)
def test_a_malformed_secret_refuses_without_echoing_it(material: str) -> None:
    """The diagnostic is the SHAPE, never the value.

    A truncated paste is a real failure mode, and an exception carrying half a private key
    is how a private key reaches a log file.
    """
    with pytest.raises(KeychainSignerError) as excinfo:
        _decode_secret(material)
    rendered = str(excinfo.value)
    assert material.strip() not in rendered or not material.strip()
    assert "not echoed" in rendered or "empty" in rendered


def test_the_refusal_for_a_wrong_length_key_does_not_contain_the_bytes() -> None:
    """The parametrised case above, made explicit for the one that carries real digits."""
    material = json.dumps(list(bytes(Keypair()))[:16])
    with pytest.raises(KeychainSignerError) as excinfo:
        _decode_secret(material)
    assert material not in str(excinfo.value)
    assert "not echoed" in str(excinfo.value)


def test_the_exception_chain_is_severed_so_no_secret_rides_the_context() -> None:
    """``from None``, not ``from exc``.

    A chained traceback can render the library's own exception, and a parsing error's
    ``args`` is the most likely place for a fragment of the material to appear.
    """
    with pytest.raises(KeychainSignerError) as excinfo:
        _decode_secret("[1, 2, 3]")
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


# --- it composes through the real signer ----------------------------------------
#
# "Wired" is not "reaches the caller". Everything above tests the backend in isolation,
# which would pass just as well if TransactionSigner could never actually drive it. N2 is
# only closed if the DEFAULT profile signs through the real seam, with the real spend gate
# and the real handoff verification in the path.


MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
REAL_BLOCKHASH = "So11111111111111111111111111111111111111112"
SLOT = 300_000_000


def _memo_tx(payer: str) -> str:
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    from gecko.landing import assemble_unsigned_tx

    meta = AccountMeta(
        pubkey=Pubkey.from_string(USDC), is_signer=False, is_writable=False
    )
    return assemble_unsigned_tx(
        [Instruction(Pubkey.from_string(MEMO_PROGRAM), b"gecko", [meta])],
        payer,
        blockhash=REAL_BLOCKHASH,
    ).tx


def _signable(payer: str) -> tuple[Any, Any, Any]:
    """A verified handoff + its receipt + an authored, permissive-for-a-memo spend gate."""
    from gecko.handoff import SignerHandoff
    from gecko.simulate import Receipt, TokenDeltaReport
    from gecko.spend_policy import (
        AllowedInstruction,
        InMemorySpendLedger,
        SpendPolicy,
        SpendPolicyGate,
        TokenCaps,
    )
    from gecko.txbind import message_binding

    tx = _memo_tx(payer)
    receipt = Receipt(
        status="pass",
        err=None,
        revert_class=None,
        units_consumed=5_000,
        sol_delta=-5_000,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
        message_binding=message_binding(tx, strength="exact"),
        binding_strength="exact",
        lookup_resolution="none",
        network="mainnet",
        observed_slot=SLOT,
        token_delta=TokenDeltaReport(status="measured", movements=(), refusals=()),
    )
    handoff = SignerHandoff(
        approved=True,
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
    gate = SpendPolicyGate(
        policy=SpendPolicy(
            authorized=True,
            per_transaction_cap_lamports=10_000_000,
            hourly_cap_lamports=100_000_000,
            daily_cap_lamports=500_000_000,
            max_transactions_per_day=100,
            allowed_instructions=frozenset(
                {AllowedInstruction(program_id=MEMO_PROGRAM, discriminator=b"gecko")}
            ),
            allowed_destinations=frozenset({USDC}),
            token_caps=TokenCaps.none(),
        ),
        ledger=InMemorySpendLedger(),
    )
    return handoff, receipt, gate


def test_the_default_profile_signs_end_to_end_through_the_real_signer() -> None:
    """N2, closed and demonstrated — not merely implemented.

    The signer is constructed with the ``os-keychain`` profile and this backend, and the
    transaction goes all the way through handoff re-verification, the receipt freshness
    checks, the spend gate, and out as a signature.
    """
    from gecko.signer import TransactionSigner

    keypair = Keypair()
    payer = str(keypair.pubkey())
    backend = OsKeychainBackend.open(REF, keyring_module=_store(keypair))
    handoff, receipt, gate = _signable(payer)

    signer = TransactionSigner(
        backend=backend,
        profile=backend.profile(network="mainnet", authorized=True),
        spend_gate=gate,
    )
    signed = signer.sign(handoff, receipt=receipt, current_slot=SLOT + 1)

    assert signed.signer_pubkey == payer
    assert signed.binding == receipt.message_binding
    assert signed.strength == "exact"
    assert signed.spend_verdict is not None and signed.spend_verdict.authorized

    # A real signature over the real message, verified by solders rather than by us.
    import base64

    Transaction.from_bytes(base64.b64decode(signed.signed_transaction_base64)).verify()


def test_the_default_profile_refuses_when_the_human_never_authorized_it() -> None:
    """Having a backend must not become having permission.

    The gap N2 named was "the default cannot sign". The fix must not overshoot into "the
    default signs", which is why ``profile()`` leaves ``authorized`` False.
    """
    from gecko.signer import SignerRefused, TransactionSigner

    keypair = Keypair()
    payer = str(keypair.pubkey())
    backend = OsKeychainBackend.open(REF, keyring_module=_store(keypair))
    handoff, receipt, gate = _signable(payer)

    signer = TransactionSigner(
        backend=backend,
        profile=backend.profile(network="mainnet"),  # authorized defaults to False
        spend_gate=gate,
    )
    with pytest.raises(SignerRefused) as excinfo:
        signer.sign(handoff, receipt=receipt, current_slot=SLOT + 1)
    assert excinfo.value.code == "not-authorized"


def test_a_foreign_fee_payer_is_refused_before_the_keychain_is_read() -> None:
    """The signer's own fee-payer check fires first, so no unlock prompt is provoked.

    Ordering matters for a keychain backend in a way it does not for a file: reading the
    key is a user-visible event. A transaction that was never going to be signed must not
    produce a password prompt.
    """
    from gecko.signer import SignerRefused, TransactionSigner

    keypair = Keypair()
    ring = _store(keypair)
    backend = OsKeychainBackend.open(REF, keyring_module=ring)
    handoff, receipt, gate = _signable(str(Keypair().pubkey()))  # somebody else pays

    reads_before = ring.reads
    signer = TransactionSigner(
        backend=backend,
        profile=backend.profile(network="mainnet", authorized=True),
        spend_gate=gate,
    )
    with pytest.raises(SignerRefused) as excinfo:
        signer.sign(handoff, receipt=receipt, current_slot=SLOT + 1)
    assert excinfo.value.code == "fee-payer-not-controlled"
    assert ring.reads == reads_before, (
        "a refused transaction must not read the keychain"
    )


# --- the package boundary -------------------------------------------------------


def test_nothing_in_gecko_imports_this_backend() -> None:
    """The one-way door. Key material lives OUTSIDE the package, always.

    ``SigningBackend``'s own contract says "Implemented OUTSIDE ``gecko/``, always", and
    ``sign_and_send.py`` opens by promising nothing in ``gecko/`` holds a key. An import
    edge from the package into this module would be the first half of breaking that.
    """
    package = Path(__file__).resolve().parent.parent / "gecko"
    offenders = [
        path.relative_to(package.parent)
        for path in package.rglob("*.py")
        if "os_keychain_backend" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"gecko/ must not reference the key holder: {offenders}"
