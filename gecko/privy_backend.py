"""The Privy signing backend — a key holder that is NOT us, behind the existing seam.

This is ``option D`` from :mod:`gecko.signer`'s header, made real: *an external signer that
holds the key and consults our verdict*. Privy holds the Solana key in its own enclave;
this module holds an app secret that lets us ASK Privy to sign, and nothing else. There is
no member here through which key material could arrive, so moving it from ``scripts/``
into the package (any-query billing, 2026-10-08) does not make ``gecko/`` a key holder:
:class:`PrivyBackend` satisfies the same two-member :class:`~gecko.signer.SigningBackend`
Protocol it always did, and ``scripts/privy_backend.py`` re-exports it unchanged.

**``signTransaction``, NEVER ``signAndSendTransaction``.** Privy offers both. The second
signs *and broadcasts*, and using it would route the send through a vendor instead of
through the caller who simulated and confirms it. This module calls ``signTransaction``
and hands the signed bytes back. The URL is built from a constant path so a caller cannot
name the other one.

**WHAT THIS BACKEND CHECKS THAT THE SEAM CANNOT.** :func:`gecko.signer._rebind` re-hashes
what comes back and refuses a backend that changed the message. That check is exactly right
and it is blind to one failure: **a binding is taken over the MESSAGE, and a signature lives
outside the message**, so a backend that returns the transaction *unsigned* — a stubbed
endpoint, a policy that declined without erroring, a proxy that echoed the request — re-binds
perfectly and reaches the caller labelled signed. :meth:`PrivyBackend.sign_transaction`
therefore reads the fee payer's signature slot out of the returned wire bytes and refuses if
it is still 64 zero bytes.

**THE ATTESTATION IS CONSULTED HERE, NOT FORWARDED.** ``signTransaction`` has no metadata
field, so our :class:`~gecko.signer.SigningAttestation` cannot travel to Privy's own policy
engine on this endpoint — a Privy policy sees the transaction, never our verdict. The
attestation is enforced *by this backend, in this process*, which is a weaker place than
inside the enclave. The two refusals below (network, fee payer) are deliberately the ones
the seam ALSO makes; neither is load-bearing alone.

**WALLET CREATION BINDS ON CREATE, NEVER ON ASSERT** (enrolment ruling, 2026-08-14).
:func:`create_solana_wallet` takes no wallet id: it asks Privy to MAKE a wallet and returns
Privy's ``(id, address)``. A ``wallet_id`` is not a secret, so an endpoint that accepted
one would let anybody who learned a victim's id bind it to their own account. A wallet is
never created unrestricted: :func:`create_solana_wallet` requires the id of a policy made
first by :func:`create_policy` (:mod:`gecko.anyquery.wallet_policy` authors it).
:mod:`gecko.anyquery.enrol` is the only caller, and it ships disabled.

**REDACTION.** The app secret and the authorization key never enter an exception, a log
line, or a repr. Every failure raises :class:`PrivyBackendError` carrying a class of
failure and, at most, Privy's HTTP status — never a request body, never a header.

**SECRETS COME FROM THE OS KEYCHAIN ONLY.** :func:`resolve_config` reads through
``ChainResolver([KeyringBackend()])`` — no command hook, no environment fallback (founder
rule: never ``.env``). Set each name with ``gecko auth set <NAME>``.

Pattern B: ``transport`` is an injected seam, so every refusal here is falsifiable offline
with no network, no Privy account and no key (``tests/test_privy_backend.py``).

Names, resolved from the keychain::

    PRIVY_APP_ID             # public app id
    PRIVY_APP_SECRET         # basic-auth secret; never logged
    PRIVY_WALLET_ID          # WHICH wallet signs (script use; billing reads the directory)
    PRIVY_AUTHORIZATION_KEY  # optional: required only if the app enforces one
    PRIVY_NETWORK            # optional
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .credentials import ChainResolver, CredentialError, CredentialRef, KeyringBackend
from .networks import UNKNOWN_NETWORK, Network, coerce_network
from .rpc import user_agent
from .signer import AUTHORITY_ROLE, SigningAttestation

__all__ = [
    "CONFIG_NAMES",
    "OPTIONAL_CONFIG_NAMES",
    "PRIVY_API_BASE",
    "SIGN_METHOD",
    "POLICY_CREATE_PATH",
    "SIGNATURE_BYTES",
    "signature_at",
    "UNCONFIRMED_POLICY_UPDATE_METHOD",
    "UNCONFIRMED_WALLET_POLICY_IDS_FIELD",
    "CreatedWallet",
    "PrivyBackend",
    "PrivyBackendError",
    "PrivyRequest",
    "Transport",
    "authorization_signature",
    "basic_headers",
    "create_policy",
    "create_solana_wallet",
    "first_signature",
    "from_env",
    "from_keychain",
    "get_wallet",
    "idempotency_key_for",
    "keychain_resolver",
    "privy_call",
    "resolve_config",
    "update_policy",
]

#: Privy's API root. A constant, not a parameter: a caller-supplied base URL on a path that
#: carries an app secret is a credential-exfiltration seam, and this file is the one place
#: the secret exists. Overridable ONLY by the injected transport, which sends nothing.
PRIVY_API_BASE = "https://api.privy.io"

#: The signing method. Named once. ``signAndSendTransaction`` is deliberately absent.
SIGN_METHOD = "signTransaction"

#: An ed25519 signature is 64 bytes; an unsigned slot is 64 zeros.
SIGNATURE_BYTES = 64

#: Seconds before a request to Privy is abandoned. A signer that hangs is a signer that
#: burns a blockhash, so this is short by intent.
TIMEOUT_SECONDS = 20


class PrivyBackendError(Exception):
    """A Privy call did not produce a signature. Carries a failure class, never a secret.

    :func:`gecko.signer._ask_backend` converts any backend fault into a refusal and prints
    only the exception's TYPE, so nothing here reaches a log through that path either. This
    type exists so the reason survives for a human reading the script's own output.
    """


@dataclass(frozen=True)
class PrivyRequest:
    """One outbound HTTP call, as data — so a test can assert what would have been sent.

    ``headers`` excludes the ``Authorization`` header by construction: the transport adds
    it. A fake transport in a test therefore never sees the secret, which is what makes the
    offline suite safe to write.
    """

    method: str
    url: str
    body: Mapping[str, Any] | None
    privy_headers: Mapping[str, str]


#: The transport seam: takes a request and the ready-made headers, returns parsed JSON.
Transport = Callable[[PrivyRequest, Mapping[str, str]], dict[str, Any]]


def _compact_u16(buf: bytes, pos: int) -> tuple[int, int]:
    """Solana's compact-u16 (shortvec). The same encoding :mod:`gecko.txbind` parses.

    Re-implemented here rather than imported from a private name: this file is a script and
    stays importable without reaching into the package's internals.
    """
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise PrivyBackendError("the signed transaction ended mid-length-prefix")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, pos
        shift += 7
        if shift > 21:
            raise PrivyBackendError(
                "the signed transaction has a malformed length prefix"
            )


def first_signature(raw: bytes) -> bytes:
    """The FEE PAYER's signature slot, read out of the wire bytes.

    The fee payer signs first by definition — it is account key 0 and signature 0 — so this
    is the slot that must be filled for the transaction to be submittable at all.
    """
    return signature_at(raw, 0)


def signature_at(raw: bytes, index: int) -> bytes:
    """Signature slot ``index`` of the wire bytes. Slot order is the order of the
    message's required signers, so an authority that is signer 1 fills slot 1."""
    count, pos = _compact_u16(raw, 0)
    if count <= index:
        raise PrivyBackendError(
            f"the signed transaction carries {count} signature slot(s), not {index + 1}"
        )
    start = pos + index * SIGNATURE_BYTES
    end = start + SIGNATURE_BYTES
    if end > len(raw):
        raise PrivyBackendError("the signed transaction is shorter than its signatures")
    return raw[start:end]


def _default_transport(
    request: PrivyRequest, headers: Mapping[str, str]
) -> dict[str, Any]:
    """The real HTTP call. The ONLY place the secret is used, and it is never returned."""
    data = (
        json.dumps(request.body).encode("utf-8") if request.body is not None else None
    )
    http = urllib.request.Request(
        request.url,
        data=data,
        headers={**headers, "user-agent": user_agent()},
        method=request.method,
    )
    try:
        with urllib.request.urlopen(http, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The STATUS only. Privy's error body can echo the request, and the request holds
        # the transaction; neither belongs in an exception that a caller may print.
        raise PrivyBackendError(f"Privy answered HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise PrivyBackendError(
            f"Privy was unreachable ({type(exc.reason).__name__})"
        ) from None
    except (ValueError, TypeError) as exc:
        raise PrivyBackendError(
            f"Privy's answer was not JSON ({type(exc).__name__})"
        ) from None


def _canonical(payload: Mapping[str, Any]) -> str:
    """RFC 8785 (JCS) canonical JSON — sorted keys, compact separators.

    Privy signs the canonical form, so a differently-ordered dict produces a signature that
    verifies against nothing. Pinned here rather than left to `json.dumps` defaults.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def authorization_signature(authorization_key: str, payload: Mapping[str, Any]) -> str:
    """ECDSA P-256 / SHA-256 over the canonical payload, DER, base64 — Privy's scheme.

    Only needed when the Privy app enforces an authorization key. The key is read from the
    argument and released with the frame: it is never stored on the instance, so it cannot
    reach a repr.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:  # pragma: no cover - environment-dependent
        raise PrivyBackendError(
            "an authorization key is configured but `cryptography` is not installed "
            "(uv sync --extra serve)"
        ) from None

    material = authorization_key.replace("wallet-auth:", "").strip()
    try:
        private_key = serialization.load_der_private_key(
            base64.b64decode(material), password=None
        )
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise TypeError("not an elliptic-curve key")
        signature = private_key.sign(
            _canonical(payload).encode("utf-8"), ec.ECDSA(hashes.SHA256())
        )
    except Exception as exc:  # noqa: BLE001 - the key is the input; never echo it
        raise PrivyBackendError(
            f"the authorization key could not sign the request ({type(exc).__name__})"
        ) from None
    return base64.b64encode(signature).decode("ascii")


@dataclass
class PrivyBackend:
    """Ask Privy to sign. Holds an app secret and a wallet id — never a key.

    ``network`` is what this backend is configured to sign FOR, and an attestation for any
    other network is refused here as well as at the seam. It defaults to ``unknown``, which
    refuses everything: absence of configuration is a refusal, matching
    :class:`~gecko.signer.SignerProfile` rather than :mod:`gecko.policy`.
    """

    wallet_id: str
    app_id: str
    #: Basic-auth secret. Never logged, never in an exception, excluded from ``repr``.
    app_secret: str
    network: Network = UNKNOWN_NETWORK
    #: Optional; required only when the Privy app enforces an authorization key.
    authorization_key: str | None = None
    #: Injected for the offline suite. ``None`` means the real HTTP transport.
    transport: Transport | None = None
    #: Cached wallet address, fetched from Privy on first use. Never guessed.
    _address: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        """Redacted by construction: a dataclass repr would print the secret."""
        return (
            f"PrivyBackend(wallet_id={self.wallet_id!r}, app_id={self.app_id!r}, "
            f"network={self.network!r}, app_secret=<redacted>)"
        )

    # -- the SigningBackend Protocol ----------------------------------------

    @property
    def pubkey(self) -> str:
        """The wallet's Solana address, ASKED FOR rather than asserted.

        Fetched from Privy once and cached. A caller-supplied address would let a typo
        produce a signature for an account nobody controls, and the seam's fee-payer check
        would compare the claim against itself.
        """
        if self._address is None:
            self._address = self._fetch_address()
        return self._address

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        """Sign via Privy, or raise. Returns the FULL serialized signed transaction."""
        if self.network == UNKNOWN_NETWORK:
            raise PrivyBackendError(
                "this backend has no network configured, and an unconfigured signer "
                "refuses: say which network these signatures are for"
            )
        if attestation.network != self.network:
            raise PrivyBackendError(
                f"this backend signs for {self.network}, and the attestation is for "
                f"{attestation.network} — a verdict about one network authorises nothing "
                f"on another"
            )
        # Which slot this signature fills. As the fee payer it is slot 0. In the
        # AUTHORITY role (x402: the facilitator pays the fee, this wallet authorises the
        # transfer) the wallet must be a required signer and must NOT be the payer.
        if attestation.signing_as == AUTHORITY_ROLE:
            if attestation.fee_payer == self.pubkey:
                raise PrivyBackendError(
                    "asked to sign as the authority, but the bytes make this wallet the "
                    "fee payer; the role is not downgraded silently"
                )
            if self.pubkey not in attestation.required_signers:
                raise PrivyBackendError(
                    "this wallet is not a required signer of these bytes, so its "
                    "signature would be one the network never asked for"
                )
            slot = attestation.required_signers.index(self.pubkey)
        else:
            if attestation.fee_payer != self.pubkey:
                raise PrivyBackendError(
                    "the transaction's fee payer is not this wallet, so this signature "
                    "would be useless and the refusal would never be about the bytes"
                )
            slot = 0

        body = {
            "method": SIGN_METHOD,
            "params": {
                "transaction": base64.b64encode(unsigned_transaction).decode("ascii"),
                "encoding": "base64",
            },
        }
        request = PrivyRequest(
            method="POST",
            url=f"{PRIVY_API_BASE}/v1/wallets/{self.wallet_id}/rpc",
            body=body,
            privy_headers={"privy-app-id": self.app_id},
        )
        answer = self._call(request)

        data = answer.get("data") if isinstance(answer, dict) else None
        signed_b64 = data.get("signed_transaction") if isinstance(data, dict) else None
        if not isinstance(signed_b64, str) or not signed_b64:
            raise PrivyBackendError(
                "Privy's answer carried no signed transaction "
                f"(method={answer.get('method')!r})"
                if isinstance(answer, dict)
                else "Privy's answer carried no signed transaction"
            )
        try:
            signed = base64.b64decode(signed_b64, validate=True)
        except Exception as exc:  # noqa: BLE001 - never echo the payload
            raise PrivyBackendError(
                f"Privy's signed transaction was not base64 ({type(exc).__name__})"
            ) from None

        # THE CHECK THE SEAM CANNOT MAKE. A binding is taken over the message and a
        # signature lives outside it, so an unsigned echo re-binds perfectly upstream.
        if signature_at(signed, slot) == bytes(SIGNATURE_BYTES):
            raise PrivyBackendError(
                "Privy returned the transaction with an EMPTY signature slot — it was not "
                "signed. This re-binds perfectly upstream, because a binding covers the "
                "message and a signature does not, so it is refused here"
            )
        return signed

    # -- internals ----------------------------------------------------------

    def _fetch_address(self) -> str:
        request = PrivyRequest(
            method="GET",
            url=f"{PRIVY_API_BASE}/v1/wallets/{self.wallet_id}",
            body=None,
            privy_headers={"privy-app-id": self.app_id},
        )
        answer = self._call(request)
        address = answer.get("address") if isinstance(answer, dict) else None
        if not isinstance(address, str) or not address:
            raise PrivyBackendError("Privy did not report an address for this wallet")
        chain = answer.get("chain_type")
        if chain is not None and chain != "solana":
            raise PrivyBackendError(
                f"this wallet is a {chain!r} wallet, not a Solana wallet"
            )
        return address

    def _call(self, request: PrivyRequest) -> dict[str, Any]:
        return privy_call(
            request,
            app_id=self.app_id,
            app_secret=self.app_secret,
            authorization_key=self.authorization_key,
            transport=self.transport,
        )


def basic_headers(app_id: str, app_secret: str) -> dict[str, str]:
    """The headers every Privy call carries. Holds the secret: never log the result."""
    basic = base64.b64encode(f"{app_id}:{app_secret}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/json",
    }


def privy_call(
    request: PrivyRequest,
    *,
    app_id: str,
    app_secret: str,
    authorization_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Build the headers (including the secret) and hand them to the transport."""
    headers: dict[str, str] = {
        **basic_headers(app_id, app_secret),
        **request.privy_headers,
    }
    # Signatures are required on non-GET requests only, and only when the app enforces
    # an authorization key. An absent key is NOT an error here: Privy rejects the call
    # if its app requires one, which is the correct place for that decision.
    if authorization_key and request.method != "GET":
        payload = {
            "version": "1",
            "method": request.method,
            "url": request.url,
            "body": request.body,
            "headers": dict(request.privy_headers),
        }
        headers["privy-authorization-signature"] = authorization_signature(
            authorization_key, payload
        )
    return (transport or _default_transport)(request, headers)


@dataclass(frozen=True)
class CreatedWallet:
    """Privy's answer to a create: its opaque handle and the address it reports."""

    wallet_id: str
    address: str


#: CONFIRMED, 9 Oct: the founder's first live create posted here and Privy answered with
#: a policy id (``testpolicy00000000000001``) and echoed the rules back.
POLICY_CREATE_PATH = "/v1/policies"
#: UNCONFIRMED: the wallet-create field that attaches a policy. The first live wallet was
#: created with it, but nothing has yet READ the wallet back and seen the id there;
#: ``update-user-policy --live`` does that read first (:func:`get_wallet`) and stops if the
#: policy is not attached.
UNCONFIRMED_WALLET_POLICY_IDS_FIELD = "policy_ids"
#: UNCONFIRMED: replacing a policy's rules is ``PATCH /v1/policies/{policy_id}`` with a
#: ``rules`` array, per Privy's API reference; never sent by this repo. An owned policy
#: also needs the owner's signature (``PRIVY_AUTHORIZATION_KEY``); ours are app-owned.
UNCONFIRMED_POLICY_UPDATE_METHOD = "PATCH"

#: Privy's ids are lower-case alphanumerics. Checked before an id enters a URL path, so
#: an id can never carry a ``/`` or a ``..`` into another endpoint.
_PRIVY_ID = re.compile(r"^[a-z0-9]{8,64}$")


def _privy_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _PRIVY_ID.match(value):
        raise PrivyBackendError(f"that is not a Privy {kind} id")
    return value


def create_policy(
    *,
    app_id: str,
    app_secret: str,
    body: Mapping[str, Any],
    idempotency_key: str,
    authorization_key: str | None = None,
    transport: Transport | None = None,
) -> str:
    """Ask Privy to store a policy; return its id. The body is authored by the caller
    (:func:`gecko.anyquery.wallet_policy.privy_policy_body`), never by a request."""
    if not idempotency_key:
        raise PrivyBackendError("a policy create needs an idempotency key")
    request = PrivyRequest(
        method="POST",
        url=f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}",
        body=dict(body),
        privy_headers={
            "privy-app-id": app_id,
            "privy-idempotency-key": idempotency_key,
        },
    )
    answer = privy_call(
        request,
        app_id=app_id,
        app_secret=app_secret,
        authorization_key=authorization_key,
        transport=transport,
    )
    policy_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(policy_id, str) or not policy_id:
        raise PrivyBackendError("Privy's policy answer carried no policy id")
    return policy_id


def create_solana_wallet(
    *,
    app_id: str,
    app_secret: str,
    idempotency_key: str,
    policy_ids: Sequence[str],
    authorization_key: str | None = None,
    transport: Transport | None = None,
) -> CreatedWallet:
    """Ask Privy to MAKE a Solana wallet, born under ``policy_ids``. Takes no wallet id,
    by construction.

    ``policy_ids`` is required and must be non-empty: a wallet Gecko can ask to sign is
    never created unrestricted, so an empty list is a refusal, not a default.
    ``idempotency_key`` makes a retried create return the first wallet instead of a
    second funded account; callers derive it from the account id (never from a body).
    The address is Privy's answer, read from the response, never supplied.
    """
    if not idempotency_key:
        raise PrivyBackendError("a wallet create needs an idempotency key")
    ids = [p for p in policy_ids if isinstance(p, str) and p]
    if not ids or len(ids) != len(policy_ids):
        raise PrivyBackendError("a wallet is never created without its policy")
    request = PrivyRequest(
        method="POST",
        url=f"{PRIVY_API_BASE}/v1/wallets",
        body={"chain_type": "solana", UNCONFIRMED_WALLET_POLICY_IDS_FIELD: ids},
        privy_headers={
            "privy-app-id": app_id,
            "privy-idempotency-key": idempotency_key,
        },
    )
    answer = privy_call(
        request,
        app_id=app_id,
        app_secret=app_secret,
        authorization_key=authorization_key,
        transport=transport,
    )
    wallet_id = answer.get("id") if isinstance(answer, dict) else None
    address = answer.get("address") if isinstance(answer, dict) else None
    chain = answer.get("chain_type") if isinstance(answer, dict) else None
    if not isinstance(wallet_id, str) or not wallet_id:
        raise PrivyBackendError("Privy's create answer carried no wallet id")
    if not isinstance(address, str) or not address:
        raise PrivyBackendError("Privy's create answer carried no address")
    if chain != "solana":
        raise PrivyBackendError("Privy created a wallet that is not a Solana wallet")
    return CreatedWallet(wallet_id=wallet_id, address=address)


def update_policy(
    *,
    app_id: str,
    app_secret: str,
    policy_id: str,
    rules: Sequence[Mapping[str, Any]],
    authorization_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Replace ``policy_id``'s rules with ``rules``; return Privy's echo of the policy.

    The rules are authored by the caller from config (never from a request). The answer
    must name the same policy and carry exactly as many rules as were sent: an echo that
    differs is an update that did not happen the way it was asked, and it is refused so
    the founder reads it rather than trusts it.
    """
    pid = _privy_id("policy", policy_id)
    if not rules:
        raise PrivyBackendError(
            "a policy is never updated to allow nothing by accident"
        )
    request = PrivyRequest(
        method=UNCONFIRMED_POLICY_UPDATE_METHOD,
        url=f"{PRIVY_API_BASE}{POLICY_CREATE_PATH}/{pid}",
        body={"rules": [dict(rule) for rule in rules]},
        privy_headers={"privy-app-id": app_id},
    )
    answer = privy_call(
        request,
        app_id=app_id,
        app_secret=app_secret,
        authorization_key=authorization_key,
        transport=transport,
    )
    if not isinstance(answer, dict) or answer.get("id") != pid:
        raise PrivyBackendError("Privy's update answer did not name the same policy")
    echoed = answer.get("rules")
    if not isinstance(echoed, list) or len(echoed) != len(rules):
        raise PrivyBackendError(
            "Privy's update answer did not carry the rules that were sent"
        )
    return answer


def get_wallet(
    *,
    app_id: str,
    app_secret: str,
    wallet_id: str,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Privy's record of one wallet (address, chain, attached policies). A read only."""
    wid = _privy_id("wallet", wallet_id)
    request = PrivyRequest(
        method="GET",
        url=f"{PRIVY_API_BASE}/v1/wallets/{wid}",
        body=None,
        privy_headers={"privy-app-id": app_id},
    )
    answer = privy_call(
        request, app_id=app_id, app_secret=app_secret, transport=transport
    )
    if not isinstance(answer, dict) or answer.get("id") not in (None, wid):
        raise PrivyBackendError("Privy answered about a different wallet")
    return answer


def idempotency_key_for(account_id: str) -> str:
    """A stable, non-reversible create key for one account: ``sha256`` of its id."""
    return hashlib.sha256(f"gecko-wallet:{account_id}".encode("utf-8")).hexdigest()


#: The names, resolved from the OS keychain. Listed once so the "what is missing" message
#: and the lookup cannot drift.
CONFIG_NAMES = ("PRIVY_APP_ID", "PRIVY_APP_SECRET", "PRIVY_WALLET_ID")
OPTIONAL_CONFIG_NAMES = ("PRIVY_AUTHORIZATION_KEY", "PRIVY_NETWORK")


def keychain_resolver() -> ChainResolver:
    """The keychain, and only the keychain: no command hook, no environment."""
    return ChainResolver([KeyringBackend()])


def resolve_config(
    env: Mapping[str, str] | None = None, *, resolver: ChainResolver | None = None
) -> dict[str, str]:
    """Read the Privy names from the OS keychain (``gecko auth set <NAME>``).

    ``env`` is an explicit mapping for the offline suite and bypasses the keychain
    entirely, so no test can reach a real one; it is never ``os.environ``. A miss is an
    empty string, never a crash: the caller reports WHICH names are missing.
    """
    if env is not None:
        return {
            name: (env.get(name) or "").strip()
            for name in CONFIG_NAMES + OPTIONAL_CONFIG_NAMES
        }
    chain = resolver if resolver is not None else keychain_resolver()
    resolved: dict[str, str] = {}
    for name in CONFIG_NAMES + OPTIONAL_CONFIG_NAMES:
        try:
            resolved[name] = (chain.resolve(CredentialRef(api=name)) or "").strip()
        except CredentialError:
            resolved[name] = ""
    return resolved


def from_keychain(
    env: Mapping[str, str] | None = None,
    *,
    network: str | None = None,
    wallet_id: str | None = None,
    resolver: ChainResolver | None = None,
) -> PrivyBackend:
    """Build a backend, or raise naming every missing name (never a value).

    ``wallet_id`` comes from the caller's own wallet DIRECTORY (billing) and overrides
    ``PRIVY_WALLET_ID``; it is never read from a request. Every name is required except
    the authorization key and the network: a backend that half-configures itself is the
    failure the "absence of configuration is a refusal" rule exists to prevent.
    """
    source = resolve_config(env, resolver=resolver)
    if wallet_id:
        source["PRIVY_WALLET_ID"] = wallet_id
    missing = [name for name in CONFIG_NAMES if not source.get(name)]
    if missing:
        raise PrivyBackendError(
            f"not configured: {', '.join(missing)} — set each with "
            f"`gecko auth set <NAME>` (OS keychain)"
        )
    resolved = coerce_network(
        network if network is not None else source.get("PRIVY_NETWORK")
    )
    return PrivyBackend(
        wallet_id=source["PRIVY_WALLET_ID"],
        app_id=source["PRIVY_APP_ID"],
        app_secret=source["PRIVY_APP_SECRET"],
        network=resolved,
        authorization_key=source.get("PRIVY_AUTHORIZATION_KEY") or None,
    )


#: The name the scripts have always imported.
from_env = from_keychain
