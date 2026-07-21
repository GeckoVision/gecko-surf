"""`gecko keys enable|disable|list` — the founder allowlist ops command.

Thin transport over ``keyauth.FileAllowlist``; these prove the wiring + that the store
holds account ids only (never a token) and the command is dispatched by ``main``.
GECKO_CONFIG_HOME redirects the store into tmp so the suite stays hermetic.
"""

from __future__ import annotations

import json

import pytest

from gecko.cli import main

ACCOUNT = "did:privy:dev-a"


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _keys_file(home):
    return home / "gecko-keys.json"


def test_enable_writes_account_and_is_idempotent(_hermetic_home, capsys):
    assert main(["keys", "enable", ACCOUNT]) == 0
    assert "Enabled" in capsys.readouterr().out
    on_disk = json.loads(_keys_file(_hermetic_home).read_text(encoding="utf-8"))
    assert on_disk == {"accounts": [ACCOUNT], "grants": {}}

    assert main(["keys", "enable", ACCOUNT]) == 0
    assert "already enabled" in capsys.readouterr().out


def test_disable_removes_account_and_is_idempotent(_hermetic_home, capsys):
    main(["keys", "enable", ACCOUNT])
    capsys.readouterr()
    assert main(["keys", "disable", ACCOUNT]) == 0
    assert "Disabled" in capsys.readouterr().out
    on_disk = json.loads(_keys_file(_hermetic_home).read_text(encoding="utf-8"))
    assert on_disk == {"accounts": [], "grants": {}}

    assert main(["keys", "disable", ACCOUNT]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_list_shows_account_ids_only(capsys):
    main(["keys", "enable", "dev-b"])
    main(["keys", "enable", "dev-a"])
    capsys.readouterr()
    assert main(["keys", "list"]) == 0
    out = capsys.readouterr().out
    assert "dev-a" in out and "dev-b" in out
    assert out.index("dev-a") < out.index("dev-b")  # sorted


def test_list_empty_gives_hint(capsys):
    assert main(["keys", "list"]) == 0
    assert "No accounts enabled" in capsys.readouterr().out


def test_blank_account_is_rejected(capsys):
    assert main(["keys", "enable", "   "]) == 2
    assert "✗" in capsys.readouterr().err


def test_keys_appears_in_help(capsys):
    main([])
    assert "keys mint|enable|disable|list" in capsys.readouterr().out
