"""The ``os-keychain`` signing backend — the declared default, made to exist.

``DEFAULT_SIGNER_PROFILE_NAME`` has been ``"os-keychain"`` since the signer seam was
written (``gecko/signer.py:165``), and it is described there as the "local-first default:
OS-level access control and an unlock prompt, no plaintext file". No backend implemented
it. ``TransactionSigner._configuration`` resolves a ``SigningBackend`` from nothing, so the
declared default refused every transaction, and the only concrete backends were the two
WEAKER profiles — a plaintext keypair file, and an external custody provider.

WHAT THAT WAS, PRECISELY. Not a safety hole: a signer with no backend refuses, and refusal
is the correct direction. It was a product gap of the specific kind that turns into a
safety hole by social pressure — the documented-secure default did not work, so the path
of least resistance for anyone trying to sign was to name
``developer-keypair-file`` and put a key on disk. A default that refuses teaches its users
to reach past it. This closes it by making the default the thing that works.

IT LIVES IN ``scripts/``, AND THAT IS NOT AN ACCIDENT. ``SigningBackend``'s own contract
says "Implemented OUTSIDE ``gecko/``, always" (``gecko/signer.py:293``), and
``sign_and_send.py`` opens by promising that "nothing in ``gecko/`` signs, holds a key, or
broadcasts, and that stays true". Possession of key material on behalf of another party is
what a custody provider IS, legally, so the package boundary is the one-way door. The
engine's half of this — proving WHICH key answers, as an opaque handle — already exists at
``gecko.credentials.resolve_key_handle`` and is all ``gecko/`` is allowed to know.

THE MATERIAL DOES NOT LIVE ON THE INSTANCE. ``LocalKeypairBackend`` reads its file once and
holds a ``Keypair`` for its lifetime, which is reasonable for a file that is already on
disk. It is not reasonable here, because the entire premise of a keychain is that the
secret is at rest and access is an event. So this backend keeps the PUBKEY — public, safe
to log — and re-fetches the secret for each signature, discarding it in a ``finally``. Two
consequences, both intended: an unlock prompt can appear per signature rather than once per
process, and a heap dump between signatures contains no key.

PINNED TO ONE BACKEND, NEVER CHAINED. Resolution goes through
:func:`~gecko.credentials.resolve_key_handle` with ``backend_name="keyring"``, which
refuses rather than falling through. The credential CHAIN (keyring -> command -> env) is
right for an API token, where a miss costs a 401; for a signing key it would mean a locked
keychain silently promotes an environment variable to the thing that signs — a downgrade
reached by accident, at the one moment nobody wanted one.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.credentials import (  # noqa: E402
    ChainResolver,
    CredentialError,
    CredentialRef,
    KeyHandle,
    KeyringBackend,
    resolve_key_handle,
)
from gecko.signer import (  # noqa: E402
    DEFAULT_SIGNER_PROFILE_NAME,
    SignerProfile,
    SigningAttestation,
)

#: The one backend a signing key may come from under this profile. A NAME, matching
#: :class:`~gecko.credentials.KeyringBackend.name`.
KEYRING_BACKEND_NAME = "keyring"


class KeychainSignerError(Exception):
    """This backend could not sign. Names the ref and the reason, never a value.

    Separate from :class:`~gecko.signer.SignerRefused` on purpose: that vocabulary belongs
    to the seam's *policy* checks, and a backend failure is not a policy answer. The signer
    maps it to ``backend-unavailable``, which is already in its closed ``RefusalCode`` set.
    """


def _decode_secret(material: str) -> Any:
    """A stored secret -> a ``solders`` keypair. Accepts the two formats a human has.

    A JSON byte array is what ``solana-keygen`` writes and what ``LocalKeypairBackend``
    already reads, so a key moved from a file into the keychain keeps working. Base58 is
    what a wallet exports. The two are unambiguous — JSON starts with ``[``.

    Raises without echoing the material. A malformed secret is a real failure mode (a
    truncated paste, a trailing newline), and its diagnostic must be the SHAPE, never the
    value: an exception carrying half a private key is how a private key reaches a log.
    """
    from solders.keypair import Keypair

    text = material.strip()
    if not text:
        raise KeychainSignerError("the stored signing key is empty")
    try:
        if text.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(text)))
        return Keypair.from_base58_string(text)
    except KeychainSignerError:
        raise
    except Exception as exc:  # noqa: BLE001 — the type varies by format; the VALUE must not escape
        raise KeychainSignerError(
            f"the stored signing key is not a usable keypair "
            f"({type(exc).__name__}); expected a solana-keygen JSON byte array or a "
            f"base58 secret key. The value is not echoed here."
        ) from None


@dataclass
class OsKeychainBackend:
    """Signs with a key held in the OS keychain. Satisfies ``SigningBackend`` structurally.

    Construct with :meth:`open`, which proves the key is reachable and derives the pubkey.
    The constructor is left plain so a test can inject a fake keyring module.
    """

    ref: CredentialRef
    #: A light fake keyring interface in tests; ``None`` imports the real library lazily.
    keyring_module: Any = None
    #: Set by :meth:`open`. Public, safe to log — this is the only key-derived value kept.
    _pubkey: str | None = None
    #: Proof that the key resolved through the pinned backend. Carries no material.
    handle: KeyHandle | None = None

    # -- construction ----------------------------------------------------------------

    @classmethod
    def open(
        cls,
        ref: CredentialRef,
        *,
        keyring_module: Any = None,
    ) -> OsKeychainBackend:
        """Prove the key is reachable, derive the pubkey, and forget the secret.

        Fails closed and fails EARLY. A backend that cannot reach its key must say so at
        configuration time, not at the moment a caller has a verified transaction in hand
        and every other check has already passed.
        """
        backend = cls(ref=ref, keyring_module=keyring_module)
        resolver = ChainResolver(backends=[backend._keyring()])
        try:
            handle = resolve_key_handle(
                ref, backend_name=KEYRING_BACKEND_NAME, resolver=resolver
            )
        except CredentialError as exc:
            # Re-raised, not wrapped-and-widened: the message already names only the slot
            # and the pinned backend.
            raise KeychainSignerError(str(exc)) from None

        keypair = _decode_secret(backend._material())
        try:
            backend._pubkey = str(keypair.pubkey())
        finally:
            del keypair
        backend.handle = handle
        return backend

    def profile(self, *, network: str, authorized: bool = False) -> SignerProfile:
        """The matching :class:`~gecko.signer.SignerProfile`, named for THIS backend.

        ``authorized`` stays ``False`` unless a human passes it. This method exists to make
        the default profile constructible with its key handle attached; it does not exist
        to authorize anything, and it cannot — the signer reads ``authorized`` and refuses
        when it is unset.
        """
        from gecko.networks import network_from_label

        return SignerProfile(
            name=DEFAULT_SIGNER_PROFILE_NAME,
            network=network_from_label(network),
            authorized=authorized,
            key=self.handle,
        )

    # -- the SigningBackend contract -------------------------------------------------

    @property
    def pubkey(self) -> str:
        """The account this backend can sign for. Compared against the fee payer."""
        if self._pubkey is None:
            raise KeychainSignerError(
                "this backend was constructed directly rather than through open(), so no "
                "key has been proven reachable; construct it with open()"
            )
        return self._pubkey

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        """Fetch, sign, discard. The secret does not outlive this call.

        ``attestation`` is read for the one thing this backend can independently check
        without holding a policy: that the fee payer it names is the account this key
        actually is. The signer checks the same thing against the DECODED message, so this
        is a second, cheaper reading of one fact rather than a substitute for it — and a
        disagreement between the two means the attestation does not describe these bytes.
        """
        if attestation.fee_payer != self.pubkey:
            raise KeychainSignerError(
                f"the attestation names fee payer {attestation.fee_payer} but this key "
                f"signs for {self.pubkey}; these are not the same account"
            )

        from solders.transaction import Transaction

        keypair = _decode_secret(self._material())
        try:
            transaction = Transaction.from_bytes(unsigned_transaction)
            message = transaction.message
            signature = keypair.sign_message(bytes(message))
            return bytes(Transaction.populate(message, [signature]))
        finally:
            del keypair

    # -- the one place material is touched -------------------------------------------

    def _keyring(self) -> KeyringBackend:
        """The keychain, through its PUBLIC contract.

        Goes through :class:`~gecko.credentials.KeyringBackend` rather than calling
        ``keyring.get_password`` with a locally-computed service name. The service-name and
        user conventions are that class's business; duplicating them here would be a second
        definition of where an entry lives, free to drift from the one ``gecko auth``
        writes with.
        """
        return KeyringBackend(module=self.keyring_module)

    def _material(self) -> str:
        """Read the secret from the keychain. The ONLY method that returns material.

        Deliberately one method, private, and called from exactly the two places that must
        have it — so "where does the key come from" has a single answer.

        A locked keychain raises the library's own secret-free error out of ``get`` rather
        than reading as a miss, which is the behaviour we want: "could not be read" and "is
        not there" are different answers and must not render as the same refusal.
        """
        backend = self._keyring()
        if not backend.available():
            raise KeychainSignerError(
                "no OS keychain is available in this process, so the os-keychain profile "
                "cannot sign here"
            )
        value = backend.get(self.ref)
        if value is None:
            raise KeychainSignerError(
                f"the signing key for {self.ref.slot()!r} is no longer in the keychain; "
                f"it was present when this backend was opened"
            )
        return value
