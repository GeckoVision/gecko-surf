"""The frozen-binary CA-bundle hook (``gecko._ca_bundle``).

Field report (darwin-arm64): the shipped PyInstaller onefile binary had no CA
store, so EVERY https call died with ``CERTIFICATE_VERIFY_FAILED`` at netguard's
TLS wrap. The fix exports ``SSL_CERT_FILE`` -> the bundled certifi ``cacert.pem``
at process start — but ONLY when frozen, ONLY when the user didn't set their own
store (the field workaround ``SSL_CERT_FILE=/etc/ssl/cert.pem`` must keep
winning), and NEVER crashing the CLI if certifi is absent. These tests pin that
decision table down offline — no frozen build needed (the pure function takes
frozen/env/where as inputs).
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

from gecko._ca_bundle import ensure_ca_bundle, resolve_ca_bundle


def _bundle(tmp_path: Path) -> str:
    """A CA bundle that exists on disk (content is irrelevant to the decision)."""
    pem = tmp_path / "cacert.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n")
    return str(pem)


# --- resolve_ca_bundle: the pure decision table -------------------------------


def test_frozen_clean_env_selects_certifi(tmp_path: Path) -> None:
    pem = _bundle(tmp_path)
    assert resolve_ca_bundle(frozen=True, env={}, certifi_where=lambda: pem) == pem


def test_user_ssl_cert_file_wins(tmp_path: Path) -> None:
    pem = _bundle(tmp_path)
    env = {"SSL_CERT_FILE": "/etc/ssl/cert.pem"}  # the field workaround
    assert resolve_ca_bundle(frozen=True, env=env, certifi_where=lambda: pem) is None


def test_user_ssl_cert_dir_wins(tmp_path: Path) -> None:
    pem = _bundle(tmp_path)
    env = {"SSL_CERT_DIR": "/etc/ssl/certs"}
    assert resolve_ca_bundle(frozen=True, env=env, certifi_where=lambda: pem) is None


def test_empty_env_value_is_not_an_override(tmp_path: Path) -> None:
    # SSL_CERT_FILE="" is a broken config, not a workaround — the bundle still wins.
    pem = _bundle(tmp_path)
    env = {"SSL_CERT_FILE": "", "SSL_CERT_DIR": ""}
    assert resolve_ca_bundle(frozen=True, env=env, certifi_where=lambda: pem) == pem


def test_not_frozen_is_untouched(tmp_path: Path) -> None:
    pem = _bundle(tmp_path)
    assert resolve_ca_bundle(frozen=False, env={}, certifi_where=lambda: pem) is None


def test_certifi_missing_is_untouched() -> None:
    assert resolve_ca_bundle(frozen=True, env={}, certifi_where=None) is None


def test_certifi_where_raising_is_untouched() -> None:
    def broken_where() -> str:
        raise RuntimeError("certifi exploded")

    assert resolve_ca_bundle(frozen=True, env={}, certifi_where=broken_where) is None


def test_bundle_path_missing_is_untouched(tmp_path: Path) -> None:
    ghost = str(tmp_path / "nope" / "cacert.pem")
    assert resolve_ca_bundle(frozen=True, env={}, certifi_where=lambda: ghost) is None


# --- ensure_ca_bundle: the process-start wrapper -------------------------------


def _clean_ssl_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def test_ensure_sets_env_when_frozen(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pem = _bundle(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _clean_ssl_env(monkeypatch)
    monkeypatch.setitem(
        sys.modules, "certifi", types.SimpleNamespace(where=lambda: pem)
    )
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == pem


def test_ensure_respects_user_override(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pem = _bundle(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _clean_ssl_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/cert.pem")
    monkeypatch.setitem(
        sys.modules, "certifi", types.SimpleNamespace(where=lambda: pem)
    )
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == "/etc/ssl/cert.pem"


def test_ensure_noop_when_not_frozen(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delattr(sys, "frozen", raising=False)
    _clean_ssl_env(monkeypatch)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ
    assert "SSL_CERT_DIR" not in os.environ


def test_ensure_never_raises_without_certifi(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _clean_ssl_env(monkeypatch)
    # None in sys.modules makes `import certifi` raise ImportError — the binary
    # must degrade to today's behavior, never crash over the hook.
    monkeypatch.setitem(sys.modules, "certifi", None)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ
