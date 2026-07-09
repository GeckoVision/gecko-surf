"""`gecko auth set|rm|list` — the thin CLI over the keychain backend.

No real OS keychain and no `keyring` install: a light fake is injected via
sys.modules. Asserts the secret is read only through getpass (never argv, never a
file under a temp $HOME), that `auth list` prints names only, that `rm` is
idempotent, and that `set` refuses (never writes plaintext) with no keychain.
"""

from __future__ import annotations

import sys

import pytest

from gecko.cli import main
from gecko.credentials import CredentialRef, KeyringBackend

SENTINEL = "SENTINEL-DO-NOT-LEAK"


class _FakeKeyring:
    """In-memory stand-in for the `keyring` module (no OS store, no network)."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> object:
        return self  # a non-null/fail backend => available()

    def set_password(self, service: str, user: str, password: str) -> None:
        self._store[(service, user)] = password

    def get_password(self, service: str, user: str) -> str | None:
        return self._store.get((service, user))

    def delete_password(self, service: str, user: str) -> None:
        del self._store[(service, user)]


def _fail_if_called(*_a: object, **_k: object) -> str:
    raise AssertionError("getpass must not be reached")


def test_auth_set_reads_secret_via_getpass_never_argv_or_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": SENTINEL)

    argv = ["auth", "set", "colosseum"]
    rc = main(argv)

    assert rc == 0
    # Stored in the (fake) keychain.
    assert fake.get_password("gecko:colosseum", "gecko") == SENTINEL
    # The secret never rode in argv (would hit ps / shell history).
    assert SENTINEL not in argv
    # The secret was never written to any file under the temp home.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(errors="ignore")
    # And never echoed to stdout.
    assert SENTINEL not in capsys.readouterr().out


def test_auth_set_refuses_without_keychain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, "keyring", raising=False)  # keyring absent
    monkeypatch.setattr("getpass.getpass", _fail_if_called)  # must refuse first

    rc = main(["auth", "set", "txodds"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "keychain" in err.lower()
    assert "GECKO_CRED_TXODDS" in err  # the env fallback is offered


def test_auth_list_prints_names_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    KeyringBackend(module=fake).store(CredentialRef(api="colosseum"), SENTINEL)

    rc = main(["auth", "list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "colosseum" in out
    assert "(keyring)" in out
    assert SENTINEL not in out  # never a value


def test_auth_list_env_refs_are_names_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.setenv("GECKO_CRED_TXODDS", SENTINEL)

    rc = main(["auth", "list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "GECKO_CRED_TXODDS" in out
    assert "(env)" in out
    assert SENTINEL not in out


def test_auth_rm_idempotent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    KeyringBackend(module=fake).store(CredentialRef(api="txodds"), "S")

    assert main(["auth", "rm", "txodds"]) == 0
    assert "Removed txodds" in capsys.readouterr().out

    assert main(["auth", "rm", "txodds"]) == 0  # idempotent
    assert "nothing to remove" in capsys.readouterr().out
