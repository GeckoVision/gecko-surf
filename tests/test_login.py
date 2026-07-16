"""`gecko login` — the email→OTP enrollment, fully offline (injected seams)."""

from __future__ import annotations


import pytest

from gecko.credentials import CredentialRef
from gecko.login import IDENTITY_REF, LoginError, load_identity, login


def _fake_post(script):
    """Return a post() that replays scripted (status, body) per URL suffix, recording calls."""
    calls = []

    def post(url, body):
        calls.append((url, body))
        for suffix, resp in script.items():
            if url.endswith(suffix):
                return resp
        return 404, {}

    post.calls = calls  # type: ignore[attr-defined]
    return post


def test_login_seals_key_and_writes_non_secret_identity(tmp_path):
    sealed: list = []
    post = _fake_post(
        {
            "/registry/keys": (202, {}),
            "/registry/keys/verify": (200, {"key": "SECRET-KEY"}),
        }
    )
    rc = login(
        "dev@example.com",
        registry_url="https://mcp.geckovision.tech",
        post=post,
        prompt=lambda q: "123456",
        store=lambda ref, secret: sealed.append((ref, secret)) or True,
        home=tmp_path,
    )
    assert rc == 0
    # the key was sealed via the identity slot
    assert sealed == [(IDENTITY_REF, "SECRET-KEY")]
    assert IDENTITY_REF == CredentialRef(api="gecko-identity")
    # the identity file exists and holds NO secret
    ident = load_identity(tmp_path)
    assert ident["email"] == "dev@example.com"
    assert ident["registry"] == "https://mcp.geckovision.tech"
    raw = (tmp_path / "identity.json").read_text()
    assert "SECRET-KEY" not in raw  # the key is NEVER written to disk
    assert "enrolled_at" in ident


def test_login_rejects_bad_email(tmp_path):
    with pytest.raises(LoginError, match="valid email"):
        login(
            "not-an-email",
            registry_url="https://mcp.geckovision.tech",
            post=_fake_post({}),
            prompt=lambda q: "x",
            store=lambda r, s: True,
            home=tmp_path,
        )


def test_login_wrong_code_raises_and_seals_nothing(tmp_path):
    sealed: list = []
    post = _fake_post(
        {"/registry/keys": (202, {}), "/registry/keys/verify": (401, {"error": "bad"})}
    )
    with pytest.raises(LoginError, match="invalid or expired"):
        login(
            "dev@example.com",
            registry_url="https://mcp.geckovision.tech",
            post=post,
            prompt=lambda q: "000000",
            store=lambda r, s: sealed.append(1) or True,
            home=tmp_path,
        )
    assert sealed == []  # never sealed a key on a failed verify
    assert not (tmp_path / "identity.json").exists()  # and never wrote identity


def test_login_no_keychain_reports_failure_without_leaking(tmp_path, capsys):
    post = _fake_post(
        {
            "/registry/keys": (202, {}),
            "/registry/keys/verify": (200, {"key": "SECRET-KEY"}),
        }
    )
    with pytest.raises(LoginError, match="no OS keychain"):
        login(
            "dev@example.com",
            registry_url="https://mcp.geckovision.tech",
            post=post,
            prompt=lambda q: "123456",
            store=lambda r, s: False,  # keychain unavailable
            home=tmp_path,
        )
    # the key never reaches stdout/stderr or disk
    out = capsys.readouterr()
    assert "SECRET-KEY" not in (out.out + out.err)
    assert not (tmp_path / "identity.json").exists()


def test_load_identity_missing_returns_none(tmp_path):
    assert load_identity(tmp_path) is None


def test_default_post_sends_real_user_agent(monkeypatch):
    """``_default_post`` must send an honest UA — never the default ``Python-urllib/*``
    that Cloudflare bans (HTTP 403 error 1010), which was the live gecko-login/Privy break."""
    from gecko import login

    captured: dict[str, str | None] = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")  # urllib title-cases the key
        return _Resp()

    monkeypatch.setattr(login, "validate_public_url", lambda url: None)  # stay offline
    monkeypatch.setattr(login.urllib.request, "urlopen", _fake_urlopen)

    status, _ = login._default_post("https://auth.privy.io/x", {"email": "a@b.co"})

    assert status == 200
    assert captured["ua"] and captured["ua"].startswith("gecko-surf/")
    assert "python-urllib" not in captured["ua"].lower()
