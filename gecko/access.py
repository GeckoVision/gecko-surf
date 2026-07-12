"""Access layer — establish an authenticated TxODDS session for an agent.

Encodes the flow the agent never has to learn:
  guest JWT  ->  on-chain subscribe (txSig)  ->  sign(txSig:leagues:jwt)  ->  activate -> apiToken

and the two-token auth it produces:
  Authorization: Bearer <session JWT>   (httpAuth)
  X-Api-Token:   <long-lived apiToken>  (apiKeyAuth)

The on-chain `subscribe` itself is out of scope here (it's a wallet-signing,
network-specific step — see scripts/). This layer takes the resulting txSig +
a `signer` and finishes the session. Transport + signer are injected, so the
whole flow is unit-testable with no network and no keys.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .credentials import ChainResolver, CredentialRef, default_resolver
from .identity import SessionIdentity
from .netguard import USER_AGENT, validate_public_url

AUTH_JWT_HEADER = "Authorization"
AUTH_APITOKEN_HEADER = "X-Api-Token"

# transport(method, url, headers, json_body) -> (status, parsed_body)
Transport = Callable[[str, str, dict, Any], tuple[int, Any]]
# signer(message_bytes) -> base64 ed25519 detached signature
Signer = Callable[[bytes], str]


@runtime_checkable
class AuthSession(Protocol):
    """The whole engine/adapter seam: produce the auth headers for a request.

    Any object with ``auth_headers() -> dict[str, str]`` is a valid session. A
    paywalled API returns its tokens; a public API returns an empty dict.
    """

    def auth_headers(self) -> dict[str, str]: ...


class AuthError(Exception):
    """Terminal auth failure after a bounded (once) self-heal — credentials are
    rejected and re-auth did not recover them.

    MUST be redacted: the message names only the endpoint/host and the HTTP status,
    NEVER an access token, refresh token, or any secret (redact-before-raise). The
    single leak suite asserts this.
    """


@runtime_checkable
class RefreshableSession(Protocol):
    """Optional, duck-typed capability ON TOP of ``AuthSession`` — a session that
    knows its own expiry and can re-establish itself.

    ``auth_headers()`` is the FROZEN seam (unchanged). ``expires_at()`` lets the
    proactive branch refresh just before expiry; ``invalidate()`` lets the reactive
    self-heal mark the session stale so the next ``auth_headers()`` re-establishes.

    A plain ``AuthSession`` (no ``invalidate``/``expires_at``) is NOT a
    ``RefreshableSession`` — the lifecycle hook is a no-op for it, so it behaves
    byte-identically to today (100% back-compat, proven by the seam-identity tests).
    """

    def auth_headers(self) -> dict[str, str]: ...
    def invalidate(self) -> None: ...
    def expires_at(self) -> float | None: ...


def is_refreshable(session: object) -> bool:
    """True iff ``session`` carries the optional refresh capability (both
    ``invalidate`` and ``expires_at`` are callable). Used by the caller's self-heal
    hook to stay a strict no-op for plain sessions — never ``isinstance`` on a
    non-runtime detail, just presence of the two extra methods."""
    return callable(getattr(session, "invalidate", None)) and callable(
        getattr(session, "expires_at", None)
    )


@dataclass
class Session:
    jwt: str
    api_token: str

    def auth_headers(self) -> dict[str, str]:
        return {
            AUTH_JWT_HEADER: f"Bearer {self.jwt}",
            AUTH_APITOKEN_HEADER: self.api_token,
        }


def activation_message(tx_sig: str, leagues: list[int], jwt: str) -> bytes:
    """The exact message TxODDS expects the wallet to sign."""
    return f"{tx_sig}:{','.join(str(x) for x in leagues)}:{jwt}".encode("utf-8")


def live_transport(
    method: str, url: str, headers: dict, json_body: Any
) -> tuple[int, Any]:
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    hdrs = dict(headers)
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


def start_guest(base_url: str, transport: Transport = live_transport) -> str:
    status, body = transport(
        "POST", f"{base_url.rstrip('/')}/auth/guest/start", {}, None
    )
    if isinstance(body, dict) and "token" in body:
        return body["token"]
    raise RuntimeError(f"guest/start did not return a token (status {status})")


def activate(
    base_url: str,
    tx_sig: str,
    leagues: list[int],
    jwt: str,
    wallet_signature_b64: str,
    transport: Transport = live_transport,
) -> str:
    status, body = transport(
        "POST",
        f"{base_url.rstrip('/')}/api/token/activate",
        {AUTH_JWT_HEADER: f"Bearer {jwt}"},
        {"txSig": tx_sig, "walletSignature": wallet_signature_b64, "leagues": leagues},
    )
    # activate returns the api token (text/plain per the spec)
    if isinstance(body, dict):
        token = body.get("token")
    else:
        token = str(body).strip()
    if not token:
        raise RuntimeError(f"activate did not return an api token (status {status})")
    return token


def establish_session(
    base_url: str,
    tx_sig: str,
    leagues: list[int],
    signer: Signer,
    transport: Transport = live_transport,
) -> Session:
    jwt = start_guest(base_url, transport)
    signature = signer(activation_message(tx_sig, leagues, jwt))
    api_token = activate(base_url, tx_sig, leagues, jwt, signature, transport)
    return Session(jwt=jwt, api_token=api_token)


def stub_session() -> Session:
    """A non-live session for recorded-mode demos (auth headers present, no real token)."""
    return Session(jwt="STUB_SESSION_JWT", api_token="STUB_API_TOKEN")


@dataclass
class NoAuthSession:
    """Adapter for public, no-auth APIs (e.g. Pegana's public reads).

    The ~empty adapter the architecture promises: the engine consumes auth as an
    opaque header dict, and a no-auth API simply yields an empty one. An empty
    ``auth_headers()`` also signals the client to hide any auth-gated operations
    from the agent (it can't satisfy them).
    """

    def auth_headers(self) -> dict[str, str]:
        return {}


def public_session() -> NoAuthSession:
    """A session for APIs whose endpoints need no auth (public reads only)."""
    return NoAuthSession()


@dataclass
class StaticHeaderSession:
    """Adapter for public APIs gated by a FIXED, publishable header — e.g. a Supabase
    publishable ``apikey`` (public by design, like a Stripe ``pk_``; printed in the
    provider's own docs).

    Non-empty ``auth_headers()`` makes the gated operation visible to the agent, while
    the value is injected only at call time and NEVER appears in the tool def
    (invariant #4 — auth is invisible to the agent). Do NOT use this for real secrets:
    those belong in a live session sourced from env, never a constant.
    """

    headers: dict[str, str]

    def auth_headers(self) -> dict[str, str]:
        return dict(self.headers)


def static_session(headers: dict[str, str]) -> StaticHeaderSession:
    """A session that injects fixed, non-secret headers (e.g. a publishable API key)."""
    return StaticHeaderSession(dict(headers))


# --- OAuth2 refresh lifecycle (the first generic RefreshableSession) ----------

# token_transport(token_url, form_fields) -> (status, parsed_json). Injected in tests
# (a light fake), so the whole refresh grant is falsifiable offline with no network.
TokenTransport = Callable[[str, dict[str, str]], tuple[int, Any]]


def live_token_transport(url: str, form: dict[str, str]) -> tuple[int, Any]:
    """Real OAuth token-endpoint POST (``application/x-www-form-urlencoded``).

    SSRF-guarded like every live fetch (the token endpoint is config, still validated).
    Returns ``(status, parsed_json)``; a non-JSON body comes back as raw text so the
    caller classifies it as a rejected refresh rather than crashing.
    """
    validate_public_url(url)
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


@dataclass
class _InMemorySecret:
    """A local-RAM credential backend for a secret already held in the process (e.g.
    a refresh token read from ``~/.dpo2u/oauth.json``). Implements the resolver's
    backend Protocol so the generic adapter keeps resolving through ``credentials.py``.

    The value is ``repr=False`` so it never surfaces in a ``repr``; the control plane
    never sees it (it lives only in this runner's memory)."""

    secret: str = field(repr=False)
    name: str = "in-memory"

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self.secret


@dataclass
class OAuth2Lifecycle:
    """Provider-agnostic OAuth2 refresh session — the first ``RefreshableSession``.

    Holds a short-lived access token + its ``exp`` in RAM (both ``repr=False``, never
    persisted) and a *reference* to the long-lived refresh token, which is resolved
    through ``credentials.py`` at refresh time and never stored on the instance or the
    control plane (invariant #1). ``auth_headers()`` refreshes proactively when the
    token is within ``leeway`` of ``exp``; ``invalidate()`` drops it so the reactive
    self-heal re-establishes on the next call. A rejected refresh raises a redacted
    ``AuthError`` (endpoint + status only — never a token).

    ``token_endpoint`` and ``refresh_ref`` are parameters, so TxODDS/dpo2u/Jupiter are
    data, not engine code (invariant #2). Header is ``Authorization: Bearer <token>``.
    """

    token_endpoint: str
    refresh_ref: CredentialRef
    resolver: ChainResolver = field(default_factory=default_resolver, repr=False)
    header_name: str = "Authorization"
    leeway: float = 60.0
    extra_form: dict[str, str] = field(default_factory=dict)
    transport: TokenTransport = field(default=live_token_transport, repr=False)
    clock: Callable[[], float] = field(default=time.time, repr=False)
    access_token: str | None = field(default=None, repr=False)
    exp: float | None = field(default=None, repr=False)

    def expires_at(self) -> float | None:
        return self.exp

    def invalidate(self) -> None:
        """Mark the session stale — the next ``auth_headers()`` re-establishes."""
        self.access_token = None
        self.exp = None

    def auth_headers(self) -> dict[str, str]:
        if self._needs_refresh():
            self._refresh()
        return {self.header_name: f"Bearer {self.access_token}"}

    def _needs_refresh(self) -> bool:
        if self.access_token is None:
            return True
        if self.exp is None:  # a token with unknown expiry never proactively refreshes
            return False
        return self.clock() + self.leeway >= self.exp

    def _refresh(self) -> None:
        # Resolve the refresh token fresh (RAM only); it is never stored on ``self``.
        refresh_token = self.resolver.resolve(self.refresh_ref)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            **self.extra_form,
        }
        try:
            status, body = self.transport(self.token_endpoint, form)
        except Exception:  # noqa: BLE001 - normalize to a redacted terminal error
            # ``from None`` drops the chained context so a lower-level error string can
            # never carry the form (which holds the refresh token) into the traceback.
            raise AuthError(
                f"token refresh transport failed for {self.token_endpoint}"
            ) from None
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise AuthError(
                f"token refresh rejected (status {status}) at {self.token_endpoint}"
            )
        self.access_token = str(body["access_token"])
        expires_in = body.get("expires_in")
        self.exp = self.clock() + float(expires_in) if expires_in else None


def oauth2_from_dpo2u(
    path: str | Path | None = None,
    transport: TokenTransport = live_token_transport,
) -> OAuth2Lifecycle:
    """Thin dpo2u shape over the generic ``OAuth2Lifecycle``.

    Reads ``~/.dpo2u/oauth.json`` (``{access_token, refresh_token, expires_at}``) and
    wires the dpo2u token endpoint. The refresh token is seeded into a local-RAM
    resolver backend (never stored on the session, never on the control plane); the
    access token + expiry seed the in-RAM state. The core stays provider-agnostic —
    this is only the endpoint + refresh-ref glue.
    """
    target = Path(path) if path is not None else Path.home() / ".dpo2u" / "oauth.json"
    data = json.loads(target.read_text())
    ref = CredentialRef(api="dpo2u")
    resolver = ChainResolver([_InMemorySecret(secret=str(data["refresh_token"]))])
    exp_raw = data.get("expires_at")
    return OAuth2Lifecycle(
        token_endpoint="https://mcp.dpo2u.com/token",
        refresh_ref=ref,
        resolver=resolver,
        transport=transport,
        access_token=(str(data["access_token"]) if data.get("access_token") else None),
        exp=(float(exp_raw) if exp_raw is not None else None),
    )


@dataclass
class ResolvedSession:
    """Live session that resolves its provider secret AT CALL TIME from the
    credential chain (keychain -> env), driven by the surface's auth MAPPING
    (control plane: which header, which scheme — never the value).

    The value is fetched fresh per call and NEVER stored on the instance, so a
    ``ResolvedSession`` is safe to hold, serialize, or ``repr``. The ``resolver``
    is excluded from ``repr`` on purpose: it is non-secret config, but keeping it
    out guarantees the session's ``repr`` is strictly non-secret regardless of a
    backend's internals (asserted by the leak suite).

    Recorded mode never constructs this (it uses ``stub_session()``); only live
    mode does — the one-code-path rule holds, diverging only at the transport edge.
    """

    ref: CredentialRef
    header_name: str  # from the surface auth mapping, e.g. "X-Api-Token"
    scheme: str = "raw"  # "raw" | "bearer" — how to render the value
    resolver: ChainResolver = field(default_factory=default_resolver, repr=False)

    def auth_headers(self) -> dict[str, str]:
        secret = self.resolver.resolve(self.ref)  # may raise CredentialError
        value = f"Bearer {secret}" if self.scheme == "bearer" else secret
        return {self.header_name: value}


# --- Keychain-backed session derived from a spec's own security scheme -------


def _header_scheme_from_spec(spec: dict[str, Any]) -> tuple[str, str] | None:
    """``(header_name, scheme)`` derived from the spec's FIRST declared
    ``securityScheme`` — never a hardcoded ``Bearer``.

    Only header-shaped schemes are supported, the same safety line
    ``tools.auth_location_is_safe`` already draws for tool visibility: ``apiKey``
    placed ``in: header`` renders the raw value; ``http`` with ``scheme: bearer``
    renders ``Bearer <value>``. Everything else (apiKey in query/cookie, oauth2,
    openIdConnect, an http scheme other than bearer) returns ``None`` so the
    caller fails closed rather than guess a wire shape ``ResolvedSession`` can't
    correctly express.
    """
    schemes = (spec.get("components") or {}).get("securitySchemes")
    if not isinstance(schemes, dict) or not schemes:
        return None
    first = next(iter(schemes.values()), None)
    if not isinstance(first, dict):
        return None
    kind = str(first.get("type", "")).lower()
    if kind == "apikey" and str(first.get("in", "")).lower() == "header":
        name = first.get("name")
        return (str(name), "raw") if isinstance(name, str) and name else None
    if kind == "http" and str(first.get("scheme", "")).lower() == "bearer":
        return "Authorization", "bearer"
    return None


def keychain_session(
    spec: dict[str, Any],
    surface: str,
    *,
    resolver: ChainResolver | None = None,
) -> tuple[AuthSession, str | None]:
    """The session ``gecko serve --auth-keychain <surface>`` runs with: a live
    ``ResolvedSession`` sourced from the credential chain (keychain -> command ->
    env, see ``credentials.default_resolver``), with ``header_name``/``scheme``
    derived from the SPEC's own first declared security scheme.

    Returns ``(session, warning)``. When the spec's scheme is missing or
    unsupported this NEVER crashes — it degrades to ``public_session()`` and
    returns a printable warning for the caller to surface (recorded-mode and
    no-auth calls keep working; a live auth-gated call fails at the transport
    edge, same as an unset ``--auth-env`` today).
    """
    mapping = _header_scheme_from_spec(spec)
    if mapping is None:
        return public_session(), (
            f"auth: could not derive a header/scheme for {surface!r} from the "
            "spec's security schemes (need an apiKey-in-header or http/bearer "
            "scheme) — serving without auth injection."
        )
    header_name, scheme = mapping
    session = ResolvedSession(
        CredentialRef(api=surface),
        header_name,
        scheme=scheme,
        resolver=resolver or default_resolver(),
    )
    return session, None


@dataclass
class GovernedSession:
    """Governance adapter: an ``AuthSession`` (usually a ``ResolvedSession``) bound
    to a ``SessionIdentity`` (the operator's policy + a non-secret free-tier id).

    The whole point is that the ``AuthSession`` seam is UNCHANGED for the caller:
    ``auth_headers()`` returns the **byte-identical** dict the underlying session
    returns. The identity/policy rides alongside as control-plane metadata — it
    never alters the wire headers. (Shape-now-token-later: once ``SessionIdentity``
    mints a per-session token, credential selection-by-policy happens HERE, still
    behind the same seam; today the identity is pass-through, so headers flow
    through untouched.)

    ``repr`` is leak-free by delegation: it prints the underlying session's own
    (non-secret) ``repr`` and the identity's non-secret id + policy shape. A
    ``ResolvedSession`` inner keeps its secret out of ``repr`` regardless; this
    adapter adds no new leak surface. Recorded mode never constructs this — only
    live mode governs a real value-moving session.
    """

    inner: AuthSession
    identity: SessionIdentity

    def auth_headers(self) -> dict[str, str]:
        # Seam held: the governed session is byte-identical on the wire. The policy
        # is consulted out-of-band (never here) so the header dict never diverges.
        return self.inner.auth_headers()
