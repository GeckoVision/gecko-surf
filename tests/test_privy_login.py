"""Privy passwordless email-OTP `gecko login` — fully offline (injected seams, no network).

Falsifies the Privy flow without a live call: a fake ``post`` seam replays scripted
``(status, body)`` per endpoint, so init/authenticate, sealing, the identity file, the
error mapping, the header set, and the redaction guarantee are all exercised with zero
network. Live smoke against real Privy is the FINAL manual check (see the module report),
never the debugger — Pattern B.
"""

from __future__ import annotations

import pytest

from gecko.credentials import CredentialRef
from gecko.login import IDENTITY_REF, LoginError, load_identity
from gecko.privy_login import (
    PRIVY_AUTH_PATH,
    PRIVY_BASE_URL,
    PRIVY_CLIENT,
    PRIVY_INIT_PATH,
    PRIVY_LOGIN_MODE,
    PrivyProvider,
    build_privy_provider,
    privy_login,
    privy_post,
)

# A distinctive secret so the leak suite can assert it never surfaces.
_TOKEN = "PRIVY-JWT-TOP-SECRET-eyJ"
_USER_ID = "did:privy:cltest0000000000000000"


def _fake_post(script):
    """A ``post(url, body)`` that replays scripted ``(status, body)`` per URL suffix and
    records every ``(url, body)`` it is called with."""
    calls = []

    def post(url, body):
        calls.append((url, body))
        for suffix, resp in script.items():
            if url.endswith(suffix):
                return resp
        return 404, {}

    post.calls = calls  # type: ignore[attr-defined]
    return post


def _ok_script(token_field: str = "privy_access_token"):
    """init 200, authenticate 200 with the token under ``token_field``."""
    return {
        PRIVY_INIT_PATH: (200, {}),
        PRIVY_AUTH_PATH: (
            200,
            {
                "user": {"id": _USER_ID},
                token_field: _TOKEN,
                "refresh_token": "refresh-xyz",
                "identity_token": "id-token-xyz",
            },
        ),
    }


def _run(script, *, prompt=lambda q: "123456", store=None, home, app_id="app-pub-123"):
    """Drive ``privy_login`` with a fake ``post`` and a recording ``store``."""
    sealed: list = []
    if store is None:

        def store(ref, secret):
            sealed.append((ref, secret))
            return True

    rc = privy_login(
        "dev@example.com",
        app_id=app_id,
        prompt=prompt,
        store=store,
        home=home,
        post=_fake_post(script),
    )
    return rc, sealed


# --- happy path -----------------------------------------------------------------------


def test_happy_path_seals_token_and_writes_non_secret_identity(tmp_path):
    rc, sealed = _run(_ok_script(), home=tmp_path)
    assert rc == 0
    # the Privy access token was sealed via the identity slot
    assert sealed == [(IDENTITY_REF, _TOKEN)]
    assert IDENTITY_REF == CredentialRef(api="gecko-identity")
    # the identity file holds only NON-SECRET references
    ident = load_identity(tmp_path)
    assert ident["email"] == "dev@example.com"
    assert ident["privy_user_id"] == _USER_ID
    assert ident["issuer"] == "privy"
    assert "enrolled_at" in ident
    # the token is NEVER written to disk
    raw = (tmp_path / "identity.json").read_text()
    assert _TOKEN not in raw


def test_token_field_fallback_to_token(tmp_path):
    # A tenant that returns only ``token`` (no ``privy_access_token``) still seals it.
    rc, sealed = _run(_ok_script(token_field="token"), home=tmp_path)
    assert rc == 0
    assert sealed == [(IDENTITY_REF, _TOKEN)]


# --- failure paths: nothing sealed, nothing written -----------------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_wrong_or_expired_code_raises_and_seals_nothing(tmp_path, status):
    sealed: list = []
    script = {PRIVY_INIT_PATH: (200, {}), PRIVY_AUTH_PATH: (status, {"error": "bad"})}
    with pytest.raises(LoginError, match="invalid or expired"):
        _run(script, home=tmp_path, store=lambda r, s: sealed.append(1) or True)
    assert sealed == []
    assert not (tmp_path / "identity.json").exists()


def test_authenticate_throttled_raises_clear_error(tmp_path):
    script = {PRIVY_INIT_PATH: (200, {}), PRIVY_AUTH_PATH: (429, {})}
    with pytest.raises(LoginError, match="too many attempts"):
        _run(script, home=tmp_path)
    assert not (tmp_path / "identity.json").exists()


def test_authenticate_5xx_raises_clear_error(tmp_path):
    script = {PRIVY_INIT_PATH: (200, {}), PRIVY_AUTH_PATH: (503, {})}
    with pytest.raises(LoginError, match="temporarily unavailable"):
        _run(script, home=tmp_path)


def test_init_5xx_raises_before_prompt(tmp_path):
    # send_code fails → we must never prompt for a code or call authenticate.
    prompted = []
    script = {
        PRIVY_INIT_PATH: (500, {}),
        PRIVY_AUTH_PATH: _ok_script()[PRIVY_AUTH_PATH],
    }
    with pytest.raises(LoginError, match="temporarily unavailable"):
        _run(script, home=tmp_path, prompt=lambda q: prompted.append(q) or "123456")
    assert prompted == []  # never reached the OTP prompt
    assert not (tmp_path / "identity.json").exists()


def test_init_throttled_raises_clear_error(tmp_path):
    script = {PRIVY_INIT_PATH: (429, {}), PRIVY_AUTH_PATH: (200, {})}
    with pytest.raises(LoginError, match="too many code requests"):
        _run(script, home=tmp_path)


def test_bad_email_rejected_before_any_network(tmp_path):
    calls = []

    def post(url, body):
        calls.append(url)
        return 200, {}

    with pytest.raises(LoginError, match="valid email"):
        privy_login(
            "not-an-email",
            app_id="app-pub-123",
            prompt=lambda q: "x",
            store=lambda r, s: True,
            home=tmp_path,
            post=post,
        )
    assert calls == []  # provider never contacted


@pytest.mark.parametrize(
    "body",
    [
        {"user": {"id": _USER_ID}},  # no token
        {"privy_access_token": _TOKEN},  # no user
        {"user": {}, "privy_access_token": _TOKEN},  # user without id
        {"user": "nope", "privy_access_token": _TOKEN},  # user not an object
    ],
)
def test_unexpected_200_shape_raises_and_seals_nothing(tmp_path, body):
    sealed: list = []
    script = {PRIVY_INIT_PATH: (200, {}), PRIVY_AUTH_PATH: (200, body)}
    with pytest.raises(LoginError, match="unexpected response"):
        _run(script, home=tmp_path, store=lambda r, s: sealed.append(1) or True)
    assert sealed == []
    assert not (tmp_path / "identity.json").exists()


def test_no_keychain_reports_failure_without_leaking(tmp_path, capsys):
    # store returns False (no keychain) → LoginError, and the token never hits disk/stdout.
    with pytest.raises(LoginError, match="no OS keychain"):
        _run(_ok_script(), home=tmp_path, store=lambda r, s: False)
    out = capsys.readouterr()
    assert _TOKEN not in (out.out + out.err)
    assert not (tmp_path / "identity.json").exists()


# --- leak suite: the token/secret never surfaces in an exception or printed output -----


def test_token_never_appears_in_exception_or_output(tmp_path, capsys):
    # Success path: token is sealed (via store) but must NOT be printed anywhere.
    rc, _ = _run(_ok_script(), home=tmp_path)
    assert rc == 0
    out = capsys.readouterr()
    assert _TOKEN not in (out.out + out.err)

    # Failure path: even when the 200 body carries the token under an unexpected shape,
    # the raised error must not echo it.
    script = {
        PRIVY_INIT_PATH: (200, {}),
        PRIVY_AUTH_PATH: (200, {"privy_access_token": _TOKEN}),  # no user.id
    }
    with pytest.raises(LoginError) as excinfo:
        _run(script, home=tmp_path)
    assert _TOKEN not in str(excinfo.value)


# --- transport / header correctness (the security crux: public app id only) ------------


def test_privy_post_sends_public_headers_only_and_no_secret():
    recorded: list = []

    def transport(url, body, *, headers=None):
        recorded.append((url, body, dict(headers or {})))
        return 200, {}

    post = privy_post("app-pub-123", transport=transport)
    post(f"{PRIVY_BASE_URL}{PRIVY_INIT_PATH}", {"email": "dev@example.com"})

    _, _, headers = recorded[0]
    assert headers["privy-app-id"] == "app-pub-123"
    assert headers["privy-client"] == PRIVY_CLIENT
    assert headers["Accept"] == "application/json"
    # HARD gate: no secret / bearer ever attached client-side.
    assert "Authorization" not in headers
    lowered = {k.lower(): v for k, v in headers.items()}
    assert "privy-app-secret" not in lowered
    assert "authorization" not in lowered
    assert not any("secret" in k for k in lowered)


def test_provider_hits_correct_endpoints_and_body():
    post = _fake_post(_ok_script())
    provider = PrivyProvider(app_id="app-pub-123", post=post)
    provider.send_code("dev@example.com")
    provider.verify_code("dev@example.com", "123456")

    (init_url, init_body), (auth_url, auth_body) = post.calls  # type: ignore[attr-defined]
    assert init_url == f"{PRIVY_BASE_URL}{PRIVY_INIT_PATH}"
    assert init_body == {"email": "dev@example.com"}
    assert auth_url == f"{PRIVY_BASE_URL}{PRIVY_AUTH_PATH}"
    assert auth_body == {
        "email": "dev@example.com",
        "code": "123456",
        "mode": PRIVY_LOGIN_MODE,
    }
    assert PRIVY_LOGIN_MODE == "login-or-sign-up"


def test_build_privy_provider_defaults_real_transport():
    # Without an injected post, the provider wires the real header-injecting seam (which is
    # SSRF-guarded) — we only assert the wiring type here, never make a call.
    provider = build_privy_provider("app-pub-123")
    assert isinstance(provider, PrivyProvider)
    assert provider.base_url == PRIVY_BASE_URL
    assert callable(provider.post)
