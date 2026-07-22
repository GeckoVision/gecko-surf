"""Hosted login service — email OTP → a minted Gecko key (SERVER-SIDE identity).

The engine behind ``POST /auth/login/start`` + ``/auth/login/verify`` (design
``private/gecko-hosted-login-design.md``). It orchestrates the two injected seams — the
server-side Privy OTP client and the key registry — and mints a Gecko key from the verified
identity, returning it ONCE. Both seams are Protocols, so the whole flow is offline-falsifiable
(Pattern B) with fakes; the HTTP layer in ``http_server`` is a thin wrapper.

A per-``(handle, IP)`` rate limit is the brute-force guard on the code. Redact-before-raise: a
code, key, secret, or identity token NEVER appears in a log, an error, or a response beyond the
single minted-key return.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .keyregistry import KeyRegistry, hash_key, mint_key
from .privy_server import PrivyServerClient, PrivyServerError, privy_server_from_env

__all__ = [
    "LoginService",
    "LoginServiceError",
    "RateLimiter",
    "build_login_service_from_env",
]

#: Default brute-force guard: at most this many attempts per (key, IP) per window.
_DEFAULT_START_MAX = 5
_DEFAULT_START_WINDOW = 3600.0
_DEFAULT_VERIFY_MAX = 5
_DEFAULT_VERIFY_WINDOW = 900.0
#: The label stamped on a minted key's registry record (non-secret provenance).
_DEFAULT_LABEL = "gecko login"
_MAX_EMAIL_LEN = 254


class LoginServiceError(Exception):
    """A login step failed. Carries the HTTP ``status`` the endpoint should return and a
    redacted, user-safe message — NEVER a code, key, secret, or raw provider body."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class RateLimiter:
    """A tiny in-memory sliding-window limiter keyed by an opaque string (``handle|ip``).

    ``hit`` records one attempt and returns ``True`` when the key is now OVER ``max_attempts``
    within ``window_seconds``. Bounded: past ``max_keys`` distinct keys, expired windows are
    swept so a key-space flood can't grow this map without bound. Holds no secret — the key is
    ``handle|ip`` metadata, never a code.
    """

    max_attempts: int
    window_seconds: float
    _clock: Callable[[], float] = time.time
    _hits: dict[str, list[float]] = field(default_factory=dict, repr=False)
    max_keys: int = 100_000

    def hit(self, key: str) -> bool:
        now = self._clock()
        floor = now - self.window_seconds
        recent = [t for t in self._hits.get(key, []) if t >= floor]
        recent.append(now)
        self._hits[key] = recent
        if len(self._hits) > self.max_keys:
            self._sweep(floor)
        return len(recent) > self.max_attempts

    def _sweep(self, floor: float) -> None:
        for key in list(self._hits):
            self._hits[key] = [t for t in self._hits[key] if t >= floor]
            if not self._hits[key]:
                del self._hits[key]


@dataclass
class LoginService:
    """Start/verify orchestration over the Privy OTP client + the key registry.

    ``start`` triggers the OTP and returns the login handle; ``verify`` validates the code,
    mints a Gecko key, stores only its hash, and returns the plaintext key ONCE. The limiters
    are injectable so a test can trip them in a few calls.
    """

    privy: PrivyServerClient
    registry: KeyRegistry
    label: str = _DEFAULT_LABEL
    start_limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(_DEFAULT_START_MAX, _DEFAULT_START_WINDOW)
    )
    verify_limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(_DEFAULT_VERIFY_MAX, _DEFAULT_VERIFY_WINDOW)
    )

    def start(self, email: str, client_ip: str) -> str:
        email = self._valid_email(email)
        if self.start_limiter.hit(f"{email}|{client_ip}"):
            raise LoginServiceError(
                "too many code requests — wait a minute and try again", status=429
            )
        try:
            login_id = self.privy.start(email)
        except PrivyServerError:
            # Redact: never echo the provider reason (fail closed, generic message).
            raise LoginServiceError(
                "could not send a code — check the email and try again", status=502
            ) from None
        if not login_id or not login_id.strip():
            raise LoginServiceError("could not start login — try again", status=502)
        return login_id

    def verify(self, login_id: str, code: str, client_ip: str) -> str:
        login_id = (login_id or "").strip()
        code = (code or "").strip()
        if not login_id or not code:
            raise LoginServiceError("login_id and code are required", status=400)
        # Rate-limit BEFORE touching Privy so a brute-force burst can't drive OTP calls.
        if self.verify_limiter.hit(f"{login_id}|{client_ip}"):
            raise LoginServiceError(
                "too many attempts — wait a minute and try again", status=429
            )
        try:
            identity = self.privy.verify(login_id, code)
        except PrivyServerError:
            raise LoginServiceError("invalid or expired code", status=401) from None
        account_id = identity.account_id()
        if not account_id:
            raise LoginServiceError("could not establish identity", status=502)
        key = mint_key()
        # Store the HASH only; the plaintext key is returned once and never persisted/logged.
        #
        # enabled=False is the whole point of self-service login: anyone who can pass an
        # email OTP gets an IDENTITY, never access. Access to a gated/paid surface is a
        # separate, deliberate founder act (`gecko keys enable` + `gecko keys grant`).
        # Minting these enabled would make every gated surface reachable by anyone with
        # an email address.
        self.registry.store_key(
            key_hash=hash_key(key),
            account_id=account_id,
            label=self.label,
            enabled=False,
        )
        return key

    def _valid_email(self, email: str) -> str:
        email = (email or "").strip()
        if "@" not in email or len(email) > _MAX_EMAIL_LEN:
            raise LoginServiceError("enter a valid email address", status=400)
        return email


def build_login_service_from_env(
    env: dict[str, str] | None = None,
) -> LoginService | None:
    """Wire the login service from env, or ``None`` when Privy OR the registry is unconfigured.

    Both the server-side Privy client (``PRIVY_APP_ID`` + ``PRIVY_APP_SECRET``) and the key
    registry (``MONGODB_URI``) must be present; otherwise the endpoints stay disabled (503)
    rather than half-work. Reuses the two modules' own env builders.
    """
    from .keyregistry import registry_from_env

    privy = privy_server_from_env(env)
    registry = registry_from_env(env)
    if privy is None or registry is None:
        return None
    return LoginService(privy=privy, registry=registry)
