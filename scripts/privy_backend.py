"""The Privy signing backend — a key holder that is NOT us, behind the existing seam.

This is ``option D`` from :mod:`gecko.signer`'s header, made real: *an external signer that
holds the key and consults our verdict*. Privy holds the Solana key in its own enclave;
this file holds an app secret that lets us ASK Privy to sign, and nothing else. There is no
member here through which key material could arrive, and ``gecko/`` still signs nothing —
:class:`PrivyBackend` lives in ``scripts/`` for the same reason ``LocalKeypairBackend``
does, and satisfies the same two-member :class:`~gecko.signer.SigningBackend` Protocol.

**``signTransaction``, NEVER ``signAndSendTransaction``.** Privy offers both. The second
signs *and broadcasts*, and using it would make "nothing in ``gecko/`` broadcasts" false by
routing the send through a vendor instead of through us — the same rule, evaded rather than
kept. This file calls ``signTransaction`` and hands the signed bytes back to the caller, who
submits them. The URL is built from a constant path so a caller cannot name the other one.

**WHAT THIS BACKEND CHECKS THAT THE SEAM CANNOT.** :func:`gecko.signer._rebind` re-hashes
what comes back and refuses a backend that changed the message. That check is exactly right
and it is blind to one failure: **a binding is taken over the MESSAGE, and a signature lives
outside the message**, so a backend that returns the transaction *unsigned* — a stubbed
endpoint, a policy that declined without erroring, a proxy that echoed the request — re-binds
perfectly and reaches the caller labelled signed. :meth:`PrivyBackend.sign_transaction`
therefore reads the fee payer's signature slot out of the returned wire bytes and refuses if
it is still 64 zero bytes. Nothing upstream can make that check, because upstream is
comparing the half of the transaction that signing does not touch.

**THE ATTESTATION IS CONSULTED HERE, NOT FORWARDED.** ``signTransaction`` has no metadata
field, so our :class:`~gecko.signer.SigningAttestation` cannot travel to Privy's own policy
engine on this endpoint — a Privy policy sees the transaction, never our verdict. Said
plainly rather than implied: the attestation is enforced *by this backend, in this process*,
which is a weaker place than inside the enclave. The two refusals below (network, fee payer)
are deliberately the ones the seam ALSO makes; a second party repeating a check is the point
of there being two parties, and neither is load-bearing alone.

**REDACTION.** The app secret and the authorization key never enter an exception, a log
line, or a repr. Every failure below raises :class:`PrivyBackendError` carrying a class of
failure and, at most, Privy's HTTP status — never a request body, never a header.

Pattern B: ``transport`` is an injected seam, so every refusal in this file is falsifiable
offline with no network, no Privy account and no key (``tests/test_privy_backend.py``). The
live call is the last check, never the debugger.

Configure it from the environment (nothing here reads a file)::

    PRIVY_APP_ID=...            # public app id
    PRIVY_APP_SECRET=...        # basic-auth secret; never logged
    PRIVY_WALLET_ID=...         # WHICH wallet signs
    PRIVY_AUTHORIZATION_KEY=... # optional: required only if the app enforces one
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.networks import UNKNOWN_NETWORK, Network, coerce_network  # noqa: E402
from gecko.rpc import user_agent  # noqa: E402
from gecko.signer import SigningAttestation  # noqa: E402

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
    count, pos = _compact_u16(raw, 0)
    if count < 1:
        raise PrivyBackendError("the signed transaction carries no signature slot")
    end = pos + SIGNATURE_BYTES
    if end > len(raw):
        raise PrivyBackendError("the signed transaction is shorter than one signature")
    return raw[pos:end]


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
        if attestation.fee_payer != self.pubkey:
            raise PrivyBackendError(
                "the transaction's fee payer is not this wallet, so this signature would "
                "be useless and the refusal would never be about the bytes"
            )

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
        if first_signature(signed) == bytes(SIGNATURE_BYTES):
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
        """Build the headers (including the secret) and hand them to the transport."""
        basic = base64.b64encode(
            f"{self.app_id}:{self.app_secret}".encode("utf-8")
        ).decode("ascii")
        headers: dict[str, str] = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/json",
            **request.privy_headers,
        }
        # Signatures are required on non-GET requests only, and only when the app enforces
        # an authorization key. An absent key is NOT an error here: Privy rejects the call
        # if its app requires one, which is the correct place for that decision.
        if self.authorization_key and request.method != "GET":
            payload = {
                "version": "1",
                "method": request.method,
                "url": request.url,
                "body": request.body,
                "headers": dict(request.privy_headers),
            }
            headers["privy-authorization-signature"] = authorization_signature(
                self.authorization_key, payload
            )
        transport = self.transport or _default_transport
        return transport(request, headers)


#: The four names, resolved identically whether they live in the OS keychain or the
#: environment. Listed once so the "what is missing" message and the lookup cannot drift.
CONFIG_NAMES = ("PRIVY_APP_ID", "PRIVY_APP_SECRET", "PRIVY_WALLET_ID")
OPTIONAL_CONFIG_NAMES = ("PRIVY_AUTHORIZATION_KEY", "PRIVY_NETWORK")


def resolve_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read the configuration through Gecko's own credential chain: keyring → command → env.

    The app secret belongs in the OS keychain, not in a shell profile, and this is the
    resolver every other Gecko credential already uses (``gecko auth set PRIVY_APP_SECRET``).
    Passing ``env`` explicitly bypasses the chain entirely — that is what the offline suite
    does, so no test can reach a real keychain.

    A miss is an empty string, never a crash: the caller reports WHICH names are missing,
    which is more useful than the first one that failed.
    """
    if env is not None:
        return {
            name: (env.get(name) or "").strip()
            for name in CONFIG_NAMES + OPTIONAL_CONFIG_NAMES
        }

    from gecko.credentials import CredentialError, CredentialRef, default_resolver

    resolver = default_resolver()
    resolved: dict[str, str] = {}
    for name in CONFIG_NAMES + OPTIONAL_CONFIG_NAMES:
        try:
            resolved[name] = (resolver.resolve(CredentialRef(api=name)) or "").strip()
        except CredentialError:
            # A miss in the keychain still lets the environment answer, and the env
            # backend is already the chain's last link — so this is a real absence.
            resolved[name] = (os.environ.get(name) or "").strip()
    return resolved


def from_env(
    env: Mapping[str, str] | None = None, *, network: str | None = None
) -> PrivyBackend:
    """Build a backend from the credential chain, or raise naming every missing variable.

    Every name is required except the authorization key and the network. A missing one is
    an error and never a default: a backend that half-configures itself is the failure
    mode the whole "absence of configuration is a refusal" rule exists to prevent.
    """
    source = resolve_config(env)
    missing = [name for name in CONFIG_NAMES if not source.get(name)]
    if missing:
        raise PrivyBackendError(
            f"not configured: {', '.join(missing)} — set each with "
            f"`gecko auth set <NAME>` (OS keychain) or export it"
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
