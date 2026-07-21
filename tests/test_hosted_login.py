"""`gecko login` hosted client — fully offline (injected ``post`` seam, no network).

Falsifies the two-step start→verify handshake against Gecko's server: the happy path seals the
returned key + writes a non-secret identity, the error mapping is redacted, and the api_key
never reaches disk or an exception (Pattern B; live smoke is the final manual check).
"""

from __future__ import annotations

import pytest

from gecko.hosted_login import (
    GECKO_ISSUER,
    LOGIN_START_PATH,
    LOGIN_VERIFY_PATH,
    HostedProvider,
    hosted_login,
)
from gecko.login import IDENTITY_REF, LoginError, load_identity

_KEY = "gecko_sk_TOP-SECRET-DO-NOT-LEAK-0000000000000000"
_SERVER = "https://mcp.geckovision.tech"


def _fake_post(script):
    calls = []

    def post(url, body):
        calls.append((url, body))
        for suffix, resp in script.items():
            if url.endswith(suffix):
                return resp
        return 404, {}

    post.calls = calls  # type: ignore[attr-defined]
    return post


def _ok_script():
    return {
        LOGIN_START_PATH: (200, {"login_id": "login-abc"}),
        LOGIN_VERIFY_PATH: (200, {"api_key": _KEY}),
    }


def _run(script, *, prompt=lambda q: "123456", store=None, home):
    sealed: list = []
    if store is None:

        def store(ref, secret):
            sealed.append((ref, secret))
            return True

    rc = hosted_login(
        "dev@example.com",
        server_url=_SERVER,
        prompt=prompt,
        store=store,
        home=home,
        post=_fake_post(script),
    )
    return rc, sealed


# --- happy path ---------------------------------------------------------------


def test_happy_path_seals_key_and_writes_non_secret_identity(tmp_path):
    rc, sealed = _run(_ok_script(), home=tmp_path)
    assert rc == 0
    assert sealed == [(IDENTITY_REF, _KEY)]
    ident = load_identity(tmp_path)
    assert ident["email"] == "dev@example.com"
    assert ident["issuer"] == GECKO_ISSUER
    assert ident["server"] == _SERVER
    # The key is NEVER written to disk.
    assert _KEY not in (tmp_path / "identity.json").read_text()


def test_verify_body_carries_the_login_id_from_start(tmp_path):
    post = _fake_post(_ok_script())
    provider = HostedProvider(server_url=_SERVER, post=post)
    provider.send_code("dev@example.com")
    provider.verify_code("dev@example.com", "123456")
    (start_url, start_body), (verify_url, verify_body) = post.calls  # type: ignore[attr-defined]
    assert start_url.endswith(LOGIN_START_PATH)
    assert start_body == {"email": "dev@example.com"}
    assert verify_url.endswith(LOGIN_VERIFY_PATH)
    assert verify_body == {"login_id": "login-abc", "code": "123456"}


# --- failure paths: nothing sealed, nothing written ---------------------------


@pytest.mark.parametrize("status", [400, 401, 403])
def test_wrong_or_expired_code_raises_and_seals_nothing(tmp_path, status):
    sealed: list = []
    script = {
        LOGIN_START_PATH: (200, {"login_id": "login-abc"}),
        LOGIN_VERIFY_PATH: (status, {}),
    }
    with pytest.raises(LoginError, match="invalid or expired"):
        _run(script, home=tmp_path, store=lambda r, s: sealed.append(1) or True)
    assert sealed == []
    assert not (tmp_path / "identity.json").exists()


def test_login_disabled_503_raises_clear_error(tmp_path):
    script = {LOGIN_START_PATH: (503, {"error": "login_disabled"})}
    with pytest.raises(LoginError, match="not enabled"):
        _run(script, home=tmp_path)


def test_verify_throttled_raises_clear_error(tmp_path):
    script = {LOGIN_START_PATH: (200, {"login_id": "x"}), LOGIN_VERIFY_PATH: (429, {})}
    with pytest.raises(LoginError, match="too many attempts"):
        _run(script, home=tmp_path)


def test_start_missing_login_id_raises_before_prompt(tmp_path):
    prompted = []
    script = {LOGIN_START_PATH: (200, {}), LOGIN_VERIFY_PATH: (200, {"api_key": _KEY})}
    with pytest.raises(LoginError, match="could not send a code"):
        _run(script, home=tmp_path, prompt=lambda q: prompted.append(q) or "123456")
    assert prompted == []  # never reached the OTP prompt
    assert not (tmp_path / "identity.json").exists()


# --- leak suite ---------------------------------------------------------------


def test_key_never_appears_in_output_or_error(tmp_path, capsys):
    rc, _ = _run(_ok_script(), home=tmp_path)
    assert rc == 0
    out = capsys.readouterr()
    assert _KEY not in (out.out + out.err)

    # Even a 200 that omits api_key must not echo any body content in the error.
    script = {LOGIN_START_PATH: (200, {"login_id": "x"}), LOGIN_VERIFY_PATH: (200, {})}
    with pytest.raises(LoginError) as excinfo:
        _run(script, home=tmp_path)
    assert _KEY not in str(excinfo.value)
