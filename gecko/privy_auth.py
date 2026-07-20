"""PrivyAccountResolver — verify a Privy access-token "Gecko key" → stable account id.

The missing piece flagged in the Layer-1 report: a REAL :data:`~gecko.keyauth.Account
Resolver` for the seam that ``keyauth.authorize`` calls. Given a presented Privy JWT
(the sealed ``gecko login`` identity token), it verifies the token cryptographically
and returns the **stable account id = the identity SUBJECT** (Privy ``sub`` — the
``did:privy:...`` user id, same value recorded as ``privy_user_id`` in ``identity.json``;
or the verified email when a tenant omits ``sub``). It is NEVER a hash of the token —
tokens rotate on refresh, the subject does not.

Verification checks all four: the signature against Privy's JWKS (RS256/ES256), the
issuer, the audience (the app id), and expiry. **Fail-closed:** ANY failure resolves to
``None`` (deny) — it never raises, never fails open. Redact-before-raise/-log: the token
is passed straight to the verifier and NEVER logged, echoed, returned, or persisted.

Pattern B (offline-falsifiable): the JWKS lookup is the injected :data:`KeySource` seam,
so tests sign a fake Privy-shaped JWT with an ephemeral keypair and verify it with ZERO
network. The real Privy JWKS URL is fetched only at runtime behind that seam, cached
(:func:`build_privy_resolver`). PyJWT lives in the ``serve`` extra (this runs only on the
hosted serve path); a plain ``gecko add`` never imports it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from .netguard import validate_public_url
from .privy_login import PRIVY_BASE_URL

__all__ = [
    "PRIVY_JWT_ISSUER",
    "KeySource",
    "PrivyAccountResolver",
    "build_privy_resolver",
    "default_jwks_url",
    "privy_resolver_from_env",
]

logger = logging.getLogger(__name__)

#: The ``iss`` claim Privy stamps into an ACCESS token. Note this differs from
#: :data:`~gecko.privy_login.PRIVY_ISSUER` (``"privy"``), which is only the label written
#: into the non-secret ``identity.json`` — this is the cryptographic issuer we verify.
PRIVY_JWT_ISSUER = "privy.io"

#: Algorithms Privy signs access tokens with. ES256 is the current default; RS256 is
#: accepted for tenants/rotations that use it. HS* (symmetric) is deliberately excluded —
#: an attacker-supplied ``alg: HS256`` must never be honored against a public key.
_ALLOWED_ALGORITHMS = ("ES256", "RS256")

#: kid (JWK key id from the token header) -> the verifying key (a PEM/JWK/key object PyJWT
#: accepts). Injected so the JWKS lookup is falsifiable offline. A source MUST NOT log,
#: echo, or persist anything about the token; it only maps a non-secret ``kid`` to a key.
KeySource = Callable[[str], Any]


def default_jwks_url(app_id: str) -> str:
    """The public Privy JWKS endpoint for ``app_id`` (per-app signing keys).

    A fixed, trusted host (``auth.privy.io``); overridable via ``PRIVY_JWKS_URL`` for a
    self-hosted/staging tenant.
    """
    return f"{PRIVY_BASE_URL}/api/v1/apps/{app_id}/jwks.json"


@dataclass(frozen=True)
class PrivyAccountResolver:
    """An :data:`~gecko.keyauth.AccountResolver`: a presented Privy JWT → stable account id.

    Callable ``(token) -> account_id | None``. Constructed with the ``app_id`` (the expected
    audience) and a :data:`KeySource` (the JWKS lookup, injected). Every verification failure
    — bad signature, wrong issuer/audience, expiry, missing claim, malformed/absent, or a key
    source that errors — returns ``None`` (fail-closed). The token is never logged or echoed.
    """

    app_id: str
    key_source: KeySource
    issuer: str = PRIVY_JWT_ISSUER
    algorithms: tuple[str, ...] = _ALLOWED_ALGORITHMS
    #: Clock-skew tolerance (seconds) for ``exp``/``iat``. Small by default; kept strict.
    leeway: int = 30

    def __call__(self, token: str) -> str | None:
        return self._resolve(token)

    def _resolve(self, token: str) -> str | None:
        if not token or not token.strip():
            return None
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None  # malformed header/token — deny (never log the token)
        if not isinstance(kid, str) or not kid:
            return None
        try:
            key = self.key_source(kid)
        except Exception:  # noqa: BLE001 - JWKS fetch/lookup failure => fail closed
            logger.warning("privy jwks key lookup failed (kid resolution)")
            return None
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self.algorithms),
                audience=self.app_id,
                issuer=self.issuer,
                leeway=self.leeway,
                # Reject a token missing any of the trust anchors rather than silently
                # skipping the check for an absent claim (fail-closed on shape too).
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            return None  # signature/issuer/audience/expiry failure — deny
        return _stable_account(claims)


def _stable_account(claims: Mapping[str, Any]) -> str | None:
    """The stable, non-secret account id from verified claims: the identity SUBJECT.

    Privy ``sub`` (the ``did:privy:...`` user id) first; a verified ``email`` as fallback
    for a tenant that omits ``sub``. NEVER derived from the token itself (tokens rotate).
    """
    for name in ("sub", "email"):
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def build_privy_resolver(*, app_id: str, jwks_url: str) -> PrivyAccountResolver:
    """Construct a resolver whose :data:`KeySource` is a cached Privy JWKS client.

    The JWKS URL is SSRF-validated once (it is a fixed trusted host, but the guard is
    cheap and keeps invariant #1 uniform), then wrapped in a ``PyJWKClient`` that fetches
    and caches signing keys at runtime. Tests never reach this path — they inject their own
    key source into :class:`PrivyAccountResolver` directly.
    """
    validate_public_url(jwks_url)  # blocks private/loopback/link-local/non-http
    # Imported lazily: PyJWKClient pulls the JWKS fetch machinery; only the hosted serve
    # path (with Privy configured) needs it. `cache_keys` avoids refetching per request.
    client = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def key_source(kid: str) -> Any:
        return client.get_signing_key(kid).key

    return PrivyAccountResolver(app_id=app_id, key_source=key_source)


def privy_resolver_from_env(
    env: Mapping[str, str] | None = None,
) -> PrivyAccountResolver | None:
    """Build the resolver from environment config, or ``None`` when Privy is not configured.

    Reuses the SAME ``PRIVY_APP_ID`` the ``gecko login`` CLI reads (``cli._cmd_login``); the
    JWKS URL defaults to :func:`default_jwks_url` and is overridable via ``PRIVY_JWKS_URL``.
    Returning ``None`` when ``PRIVY_APP_ID`` is unset lets the gate stay fail-closed
    (``deny_all_resolver``) rather than guess — configuration presence is the on-switch.
    """
    source = os.environ if env is None else env
    app_id = (source.get("PRIVY_APP_ID") or "").strip()
    if not app_id:
        return None
    jwks_url = (source.get("PRIVY_JWKS_URL") or "").strip() or default_jwks_url(app_id)
    return build_privy_resolver(app_id=app_id, jwks_url=jwks_url)
