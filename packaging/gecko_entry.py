"""PyInstaller entry point for the standalone ``gecko`` binary.

This is the frozen-binary equivalent of the ``gecko`` console script
(``gecko.cli:_run`` — the serve/test/from-docs dispatcher). PyInstaller needs a real
module path to point ``--onefile`` at; the console-script entry point is invisible to
it. Keep this file a pure shim — all real logic stays in the package (``gecko.cli``).

Ordering is load-bearing. The CA-bundle env MUST be exported before ANY ``gecko`` import
runs, because importing the package eagerly pulls in the engine (``gecko/__init__.py``
imports access/client/mcp_server). Importing ``gecko._ca_bundle`` to get the hook would
*itself* drag the engine in first — so the frozen-CA decision is INLINED here with NO
``gecko.*`` import. ``gecko/_ca_bundle.py`` stays the unit-tested source of this decision
table; ``tests/test_frozen_entry.py`` asserts this inlined copy stays in lockstep with it
and that importing the engine never runs the hook (or builds an SSL context) on its own.
"""

from __future__ import annotations

import os
import sys


def _apply_frozen_ca_bundle() -> None:
    """Point OpenSSL at the bundled certifi ``cacert.pem`` when frozen; no-op otherwise.

    A PyInstaller onefile binary carries libssl whose compiled-in default verify paths
    point at the *build* machine's OpenSSL dir, which usually doesn't exist on a user's
    machine (the darwin-arm64 field failure: every https call died
    ``CERTIFICATE_VERIFY_FAILED`` at netguard's TLS wrap). ``--collect-data certifi`` ships
    ``cacert.pem`` inside the binary; exporting ``SSL_CERT_FILE`` makes every SSL context
    created afterwards (urllib's per-request contexts included) trust that bundled store.

    Inlined, dependency-free mirror of ``gecko._ca_bundle.resolve_ca_bundle`` /
    ``ensure_ca_bundle`` — deliberately NO ``gecko`` import (see the module docstring).
    Never raises: an absent/broken certifi degrades to the un-hooked binary's behavior.
    """
    if not getattr(sys, "frozen", False):
        return  # only a frozen binary lacks a usable system store
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return  # a user-set store always wins (an empty value is treated as unset)
    try:
        import certifi

        path = certifi.where()
    except Exception:
        return  # certifi not bundled / broken -> behave exactly as an un-hooked binary
    if path and os.path.exists(path):
        os.environ["SSL_CERT_FILE"] = path


def main() -> None:
    # TLS first: export the CA env BEFORE the first ``gecko`` import. Importing the package
    # eagerly imports the engine, and any SSL context built thereafter must see the bundled
    # store; gecko builds contexts per-request only, so this is early enough.
    _apply_frozen_ca_bundle()

    # Imported here (not at module scope) so NO gecko import precedes the CA hook. The
    # build passes `--hidden-import gecko.cli`, so PyInstaller bundles it regardless.
    from gecko.cli import _run

    _run()  # raises SystemExit(gecko.cli.main())


if __name__ == "__main__":
    main()
