"""Frozen-binary CA bundle — point OpenSSL at the certifi store PyInstaller ships.

Why this exists: a PyInstaller onefile binary carries Python's ``_ssl``/libssl,
whose compiled-in default verify paths point at the *build* machine's OpenSSL
directory. On a user's machine that path usually doesn't exist — the darwin-arm64
field report: EVERY https call died with ``CERTIFICATE_VERIFY_FAILED`` at
netguard's TLS wrap before a single byte was sent. The standard fix is to bundle
certifi's ``cacert.pem`` into the binary (``--collect-data certifi`` at build
time) and export ``SSL_CERT_FILE`` at process start — OpenSSL reads that env var
whenever it loads default verify paths, so every SSL context created afterwards
(urllib's per-request contexts in netguard included) trusts the bundled store.

Ordering: ``ensure_ca_bundle()`` must run before any SSL context is created.
Gecko creates contexts per-request, never at import time, so calling this first
thing in the frozen entry point (``packaging/gecko_entry.py``) is early enough.

Sovereignty rules (the decision table ``resolve_ca_bundle`` encodes, pure and
unit-testable):

- Only a frozen binary needs the rescue — a normal install finds the system store.
- A user-set ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` always wins (the field
  workaround ``SSL_CERT_FILE=/etc/ssl/cert.pem`` must keep working).
- certifi missing or its bundle absent on disk => do nothing; the binary behaves
  exactly as today. The hook may never crash the CLI.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping


def resolve_ca_bundle(
    *,
    frozen: bool,
    env: Mapping[str, str],
    certifi_where: Callable[[], str] | None,
    exists: Callable[[str], bool] = os.path.exists,
) -> str | None:
    """Decide the CA-bundle path to export as ``SSL_CERT_FILE`` (``None`` = leave
    the environment alone).

    Pure decision logic — ``frozen``/``env``/``certifi_where`` are inputs so the
    whole table is falsifiable offline, no frozen build needed. An *empty* env
    value is treated as unset: ``SSL_CERT_FILE=""`` is a broken config, not a
    user override.
    """
    if not frozen:
        return None
    if env.get("SSL_CERT_FILE") or env.get("SSL_CERT_DIR"):
        return None  # user override wins, always
    if certifi_where is None:
        return None
    try:
        path = certifi_where()
    except Exception:
        # A broken certifi must degrade to today's behavior, never crash the CLI.
        return None
    if not path or not exists(path):
        return None
    return path


def ensure_ca_bundle() -> None:
    """Export ``SSL_CERT_FILE`` for a frozen binary; no-op otherwise. Never raises.

    Call this at process start, before anything creates an SSL context.
    """
    frozen = bool(getattr(sys, "frozen", False))
    certifi_where: Callable[[], str] | None = None
    if frozen:
        try:
            import certifi

            certifi_where = certifi.where
        except Exception:
            certifi_where = None  # not bundled -> behave exactly as today
    path = resolve_ca_bundle(frozen=frozen, env=os.environ, certifi_where=certifi_where)
    if path is not None:
        os.environ["SSL_CERT_FILE"] = path
