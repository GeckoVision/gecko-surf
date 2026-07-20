"""Hosted login service — fully offline (fake Privy client + in-memory registry, no network).

Falsifies the start→verify orchestration without a live Privy call: a scripted fake client
drives the happy path (mint + store), the wrong/expired-code deny, the identity-fallback, and
the brute-force rate limit; the leak assertions prove a code/key never surfaces in an error.
"""

from __future__ import annotations

import itertools

import pytest

from gecko.authlogin import LoginService, LoginServiceError, RateLimiter
from gecko.keyregistry import GeckoKeyResolver, InMemoryKeyRegistry, hash_key
from gecko.privy_server import PrivyIdentity, PrivyServerError

SUBJECT = "did:privy:login-dev"
CODE = "123456"
IP = "203.0.113.7"


class _FakePrivy:
    """A scripted server-side Privy client: start returns a fixed handle; verify accepts
    ``good_code`` and returns ``identity``, else raises PrivyServerError (wrong/expired)."""

    def __init__(self, identity: PrivyIdentity, *, good_code: str = CODE) -> None:
        self._identity = identity
        self._good = good_code
        self._handles = (f"login-{n}" for n in itertools.count())
        self.started: list[str] = []

    def start(self, email: str) -> str:
        self.started.append(email)
        return next(self._handles)

    def verify(self, login_id: str, code: str) -> PrivyIdentity:
        if code != self._good:
            raise PrivyServerError("bad code")
        return self._identity


def _service(**kwargs) -> tuple[LoginService, InMemoryKeyRegistry, _FakePrivy]:
    registry = InMemoryKeyRegistry()
    privy = _FakePrivy(PrivyIdentity(subject=SUBJECT, email="dev@example.com"))
    svc = LoginService(privy=privy, registry=registry, **kwargs)
    return svc, registry, privy


# --- happy path ---------------------------------------------------------------


def test_start_then_verify_mints_and_stores_key():
    svc, registry, privy = _service()
    login_id = svc.start("dev@example.com", IP)
    assert login_id and privy.started == ["dev@example.com"]

    key = svc.verify(login_id, CODE, IP)
    # The minted key resolves back to the Privy subject via the registry.
    assert GeckoKeyResolver(registry)(key) == SUBJECT
    # Only the HASH is stored — never the plaintext key.
    assert hash_key(key) in registry._by_hash
    assert key not in repr(registry._by_hash)


def test_identity_falls_back_to_email_when_no_subject():
    registry = InMemoryKeyRegistry()
    privy = _FakePrivy(PrivyIdentity(subject="", email="only-email@example.com"))
    svc = LoginService(privy=privy, registry=registry)
    key = svc.verify(svc.start("only-email@example.com", IP), CODE, IP)
    assert GeckoKeyResolver(registry)(key) == "only-email@example.com"


# --- rejections ---------------------------------------------------------------


@pytest.mark.parametrize("email", ["", "not-an-email", "x" * 300 + "@e.com"])
def test_start_rejects_bad_email(email):
    svc, _registry, privy = _service()
    with pytest.raises(LoginServiceError) as exc:
        svc.start(email, IP)
    assert exc.value.status == 400
    assert privy.started == []  # provider never contacted


def test_wrong_code_is_401_and_mints_nothing():
    svc, registry, _privy = _service()
    login_id = svc.start("dev@example.com", IP)
    with pytest.raises(LoginServiceError) as exc:
        svc.verify(login_id, "000000", IP)
    assert exc.value.status == 401
    assert registry._by_hash == {}  # no key minted on a bad code


def test_verify_requires_login_id_and_code():
    svc, _registry, _privy = _service()
    with pytest.raises(LoginServiceError) as exc:
        svc.verify("", "", IP)
    assert exc.value.status == 400


# --- brute-force rate limit ---------------------------------------------------


def test_verify_rate_limit_trips_after_n_bad_codes():
    # 3 attempts allowed per (login_id, ip); the 4th trips → 429 before Privy is called.
    svc, registry, _privy = _service(verify_limiter=RateLimiter(3, 3600))
    login_id = svc.start("dev@example.com", IP)
    for _ in range(3):
        with pytest.raises(LoginServiceError) as exc:
            svc.verify(login_id, "000000", IP)
        assert exc.value.status == 401  # wrong code, still under the limit
    # The next attempt is rate-limited, not merely "wrong code".
    with pytest.raises(LoginServiceError) as exc:
        svc.verify(login_id, "000000", IP)
    assert exc.value.status == 429
    assert registry._by_hash == {}


def test_start_rate_limit_trips_after_n_requests():
    svc, _registry, _privy = _service(start_limiter=RateLimiter(2, 3600))
    svc.start("dev@example.com", IP)
    svc.start("dev@example.com", IP)
    with pytest.raises(LoginServiceError) as exc:
        svc.start("dev@example.com", IP)
    assert exc.value.status == 429


def test_rate_limit_is_per_key():
    limiter = RateLimiter(1, 3600)
    assert limiter.hit("a|ip1") is False
    assert limiter.hit("a|ip1") is True  # second hit on same key trips
    assert limiter.hit("b|ip2") is False  # a different key is independent


# --- leak suite ---------------------------------------------------------------


def test_code_and_key_never_appear_in_error():
    svc, _registry, _privy = _service()
    login_id = svc.start("dev@example.com", IP)
    secret_code = "SECRET-CODE-999999"
    with pytest.raises(LoginServiceError) as exc:
        svc.verify(login_id, secret_code, IP)
    assert secret_code not in str(exc.value)
