"""Layer 1, the missing piece: verify a Privy access-token "Gecko key" and resolve it
to a stable account id (the identity SUBJECT — Privy ``sub``), fail-closed.

Offline (Pattern B): each test mints an EPHEMERAL keypair, signs a Privy-shaped JWT
with it, and injects a key source that returns the matching public key — NO network.
The real Privy JWKS URL is fetched only at runtime behind that same injected seam.

The recurring assertions: any verification failure (expiry, wrong issuer, wrong
audience, tampered signature, malformed/absent) resolves to ``None`` (never raises,
never fails open), and the raw token NEVER appears in a return value or the log.
"""

from __future__ import annotations

import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from gecko.privy_auth import (
    PRIVY_JWT_ISSUER,
    PrivyAccountResolver,
    default_jwks_url,
    privy_resolver_from_env,
)

APP_ID = "clzabc123appid"
SUBJECT = "did:privy:enabled-dev-xyz"
KID = "test-key-1"


def _ec_keypair() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _rsa_keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _resolver(public_key, *, app_id: str = APP_ID, issuer: str = PRIVY_JWT_ISSUER):
    """A resolver whose key source returns ``public_key`` for the signing kid (no net)."""
    return PrivyAccountResolver(
        app_id=app_id,
        key_source=lambda _kid: public_key,
        issuer=issuer,
    )


def _sign(
    private_key,
    *,
    alg: str = "ES256",
    sub: str = SUBJECT,
    aud: str = APP_ID,
    iss: str = PRIVY_JWT_ISSUER,
    exp_delta: int = 3600,
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_delta,
        "sid": "session-123",
        **(extra or {}),
    }
    return jwt.encode(claims, private_key, algorithm=alg, headers={"kid": KID})


# --- the happy paths: ES256 + RS256 -> the stable subject --------------------


def test_valid_es256_returns_subject():
    key = _ec_keypair()
    token = _sign(key, alg="ES256")
    assert _resolver(key.public_key())(token) == SUBJECT


def test_valid_rs256_returns_subject():
    key = _rsa_keypair()
    token = _sign(key, alg="RS256")
    assert _resolver(key.public_key())(token) == SUBJECT


def test_falls_back_to_verified_email_when_no_sub():
    # A Privy tenant token without a `sub` but with a verified email claim: the email is
    # the stable account id (still the identity subject, never a hash of the token).
    key = _ec_keypair()
    token = _sign(key, sub="", extra={"email": "dev@example.com"})
    assert _resolver(key.public_key())(token) == "dev@example.com"


# --- every failure resolves to None (fail-closed, never raises) --------------


def test_expired_returns_none():
    key = _ec_keypair()
    token = _sign(key, exp_delta=-3600)  # expired an hour ago (beyond leeway)
    assert _resolver(key.public_key())(token) is None


def test_wrong_issuer_returns_none():
    key = _ec_keypair()
    token = _sign(key, iss="https://evil.example")
    assert _resolver(key.public_key())(token) is None


def test_wrong_audience_returns_none():
    key = _ec_keypair()
    token = _sign(key, aud="some-other-app-id")
    assert _resolver(key.public_key())(token) is None


def test_tampered_signature_returns_none():
    key = _ec_keypair()
    token = _sign(key)
    head, payload, _sig = token.split(".")
    other = _ec_keypair()
    forged_sig = _sign(other).split(".")[2]  # a valid-shaped sig from a DIFFERENT key
    tampered = f"{head}.{payload}.{forged_sig}"
    assert _resolver(key.public_key())(tampered) is None


def test_signed_by_unknown_key_returns_none():
    signer = _ec_keypair()
    token = _sign(signer)
    # The resolver's key source hands back a DIFFERENT public key -> signature check fails.
    assert _resolver(_ec_keypair().public_key())(token) is None


@pytest.mark.parametrize("token", ["", "   ", "not-a-jwt", "a.b", "a.b.c.d"])
def test_malformed_or_absent_returns_none(token):
    key = _ec_keypair()
    assert _resolver(key.public_key())(token) is None


def test_missing_required_claim_returns_none():
    # A token with no `exp` must be rejected (require exp/iss/aud), never accepted.
    key = _ec_keypair()
    now = int(time.time())
    token = jwt.encode(
        {"sub": SUBJECT, "aud": APP_ID, "iss": PRIVY_JWT_ISSUER, "iat": now},
        key,
        algorithm="ES256",
        headers={"kid": KID},
    )
    assert _resolver(key.public_key())(token) is None


def test_key_source_failure_fails_closed():
    # A JWKS fetch error (network down / kid not found) must deny, not raise.
    def boom(_kid):
        raise RuntimeError("jwks unreachable")

    resolver = PrivyAccountResolver(app_id=APP_ID, key_source=boom)
    key = _ec_keypair()
    assert resolver(_sign(key)) is None


# --- the token never leaks ----------------------------------------------------


def test_token_never_logged_on_any_path(caplog):
    key = _ec_keypair()
    good = _sign(key)
    bad = _sign(key, iss="https://evil.example")
    resolver = _resolver(key.public_key())
    with caplog.at_level(logging.DEBUG):
        resolver(good)
        resolver(bad)
        resolver("garbage.token.value")
    for token in (good, bad, "garbage.token.value"):
        assert token not in caplog.text


# --- env wiring: config presence flips the resolver on -----------------------


def test_privy_resolver_from_env_absent_when_no_app_id():
    assert privy_resolver_from_env(env={}) is None
    assert privy_resolver_from_env(env={"PRIVY_APP_ID": "  "}) is None


def test_privy_resolver_from_env_present_with_app_id():
    resolver = privy_resolver_from_env(env={"PRIVY_APP_ID": APP_ID})
    assert isinstance(resolver, PrivyAccountResolver)
    assert resolver.app_id == APP_ID
    assert resolver.issuer == PRIVY_JWT_ISSUER


def test_default_jwks_url_derives_from_app_id():
    url = default_jwks_url(APP_ID)
    assert APP_ID in url
    assert url.startswith("https://")
    assert url.endswith("jwks.json")
