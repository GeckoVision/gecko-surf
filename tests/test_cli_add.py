"""`gecko add` CLI-level tests — the ``_store`` closure's guard around a mid-write
keychain failure (Finding 3: a locked/broken keychain must never crash `gecko add`
or leak the secret).
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from gecko import cli, credentials

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Widget API", "version": "1"},
    "components": {
        "securitySchemes": {
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
        }
    },
    "paths": {},
}


def _write_spec(tmp_path: Path) -> str:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_SPEC), encoding="utf-8")
    return str(path)


class _MidWriteFailBackend:
    """A fake KeyringBackend: reports available, but the write itself blows up —
    the exact gap Finding 3 closes (available() True, then store() raises)."""

    def available(self) -> bool:
        return True

    def store(self, ref: credentials.CredentialRef, secret: str) -> None:
        raise OSError("simulated disk full mid-write")


def test_add_survives_mid_write_keychain_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(credentials, "KeyringBackend", _MidWriteFailBackend)
    monkeypatch.setattr(getpass, "getpass", lambda *_a, **_k: "sk-live-secret")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    rc = cli.main(["add", _write_spec(tmp_path), "--name", "widget-api"])
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    assert rc == 0  # never crashes out of _cmd_add
    assert "sealed" not in out.lower()  # never falsely claims success
    assert "sk-live-secret" not in out and "sk-live-secret" not in err  # never leaked
    assert "add later" in out.lower()  # degrades like any other failed store()
