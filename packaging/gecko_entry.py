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


def _ca_selftest_line() -> str:
    """Build ONE machine-parseable line describing the bundled certifi THIS process resolved.

    Format: ``GECKO_CA path=<abs> exists=<0|1> bytes=<n> frozen=<0|1>`` — where ``path`` is
    ``certifi.where()`` (the bundled ``cacert.pem`` inside the PyInstaller ``_MEIPASS``
    extraction dir when frozen), ``exists``/``bytes`` come from an in-process ``os.stat``,
    and ``frozen`` is ``sys.frozen``. ``bytes=-1`` means "no size available" (path absent or
    unstattable), distinct from ``bytes=0`` (a real but empty file).

    Computed IN-PROCESS on purpose: ``_MEIPASS`` is a temp dir PyInstaller deletes on exit,
    so CI can't stat the cert from outside — the running binary must report it. The gate
    (``packaging/ca_selftest_check.py``, run on every release matrix leg) parses this line
    and fails the build unless it proves the BUNDLED cert shipped + resolved. This is the
    per-arch positive assertion for macOS + linux-arm64, where the cert-stripped Docker gate
    (linux-x86_64 only) can't run. Dependency-free (no ``gecko`` import) and never raises.
    """
    frozen = 1 if getattr(sys, "frozen", False) else 0
    try:
        import certifi

        path = certifi.where() or ""
    except Exception:
        # certifi not bundled / broken -> report an absent bundle, never crash.
        path = ""
    exists = 0
    nbytes = -1
    if path:
        try:
            nbytes = os.stat(path).st_size
            exists = 1
        except OSError:
            exists, nbytes = 0, -1
    return f"GECKO_CA path={path} exists={exists} bytes={nbytes} frozen={frozen}"


def main() -> None:
    # TLS first: export the CA env BEFORE the first ``gecko`` import. Importing the package
    # eagerly imports the engine, and any SSL context built thereafter must see the bundled
    # store; gecko builds contexts per-request only, so this is early enough.
    _apply_frozen_ca_bundle()

    # Hidden CA self-test (release-CI gate, see _ca_selftest_line). OFF unless
    # GECKO_CA_SELFTEST=1, so normal CLI runs are completely untouched: print one line and
    # exit 0 WITHOUT importing/dispatching the CLI. Strict `== "1"` so no stray truthy value
    # trips it. Placed before the gecko import to keep this path dependency-free.
    if os.environ.get("GECKO_CA_SELFTEST") == "1":
        print(_ca_selftest_line())
        return

    # Imported here (not at module scope) so NO gecko import precedes the CA hook. The
    # build passes `--hidden-import gecko.cli`, so PyInstaller bundles it regardless.
    from gecko.cli import _run

    _run()  # raises SystemExit(gecko.cli.main())


if __name__ == "__main__":
    main()
