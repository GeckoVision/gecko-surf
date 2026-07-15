"""PyInstaller entry point for the standalone ``gecko`` binary.

This is the frozen-binary equivalent of the ``gecko`` console script
(``gecko.cli:_run`` — the serve/test/from-docs dispatcher). PyInstaller needs a real
module path to point ``--onefile`` at; the console-script entry point is invisible to
it. Keep this file a pure shim — all logic stays in the package (``gecko.cli``).
"""

# TLS first: a frozen binary can't find the build machine's CA store (darwin-arm64
# field report — every https call failed CERTIFICATE_VERIFY_FAILED). Point OpenSSL
# at the bundled certifi store BEFORE importing anything that could create an SSL
# context; gecko creates contexts per-request only, so this ordering is sufficient.
from gecko._ca_bundle import ensure_ca_bundle

ensure_ca_bundle()

from gecko.cli import _run  # noqa: E402  — must import AFTER the CA hook runs

if __name__ == "__main__":
    _run()
