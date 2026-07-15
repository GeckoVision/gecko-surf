"""Gate ONE ``GECKO_CA`` self-test line emitted by a frozen ``gecko`` binary.

Usage (``.github/workflows/release.yaml``, on EVERY build-matrix leg):

    GECKO_CA_SELFTEST=1 ./gecko-<os>-<arch> > ca_selftest.out
    python packaging/ca_selftest_check.py ca_selftest.out   # or pipe on stdin

Exit 0 iff the line proves the binary SHIPPED and RESOLVED the bundled certifi
store: it is the frozen binary, the cert exists on disk, its size is plausible for
a real CA bundle (~200 KB), and its path is INSIDE the PyInstaller ``_MEIPASS``
extraction dir — NOT the runner's own system/keychain trust store. This is the
build-machine-independent positive assertion that covers macOS-arm64 and
linux-arm64, where the cert-stripped Docker gate (linux-x86_64 only) cannot run.

Pure + import-light so the decision table is unit-testable (``tests/test_ca_selftest.py``
loads this by path). ``packaging/`` is not a package — its name clashes with the
PyPI ``packaging`` lib — so this is always run/loaded by file path, never imported.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

# A real cacert.pem is ~200 KB. Anything at/under this floor is an empty, truncated, or
# absent bundle (a silently CA-less binary) and must fail the build. Exclusive floor.
MIN_CA_BYTES = 50_000

# The marker PyInstaller stamps into its onefile extraction dir (``_MEIxxxxxx``). The
# bundled cacert.pem resolves under this at runtime; a real system store never contains it.
MEI_MARKER = "_MEI"

# System / keychain trust-store locations. Proving the resolved path is NOT one of these
# proves TLS would use the BUNDLED cert, not the build/runner machine's own store. Checked
# after normalizing a leading ``/private`` (macOS symlinks: /etc -> /private/etc,
# /var -> /private/var), so a legitimate _MEIPASS under /private/var/folders is NOT mistaken
# for a system store, while /private/etc/ssl still normalizes back onto a system prefix.
SYSTEM_PREFIXES = (
    "/etc/ssl",
    "/etc/pki",
    "/usr/lib/ssl",
    "/usr/share/ca-certificates",
    "/usr/local/etc/openssl",
    "/opt/homebrew/etc/openssl",
    "/System",
    "/Library",
)

_LINE_RE = re.compile(
    r"GECKO_CA path=(?P<path>\S*) exists=(?P<exists>[01]) "
    r"bytes=(?P<bytes>-?\d+) frozen=(?P<frozen>[01])"
)


class SelftestFormatError(ValueError):
    """The ``GECKO_CA`` self-test line was missing or malformed."""


@dataclass(frozen=True)
class Selftest:
    path: str
    exists: bool
    nbytes: int
    frozen: bool


def parse_line(text: str) -> Selftest:
    """Extract and parse the single ``GECKO_CA ...`` line out of the binary's output.

    Tolerant of banners/warnings around it (scans for the marker line); the last match
    wins. Raises ``SelftestFormatError`` if no well-formed line is present.
    """
    match = None
    for raw in text.splitlines():
        if raw.startswith("GECKO_CA "):
            match = _LINE_RE.fullmatch(raw.strip())
            if match is None:
                raise SelftestFormatError(f"malformed GECKO_CA line: {raw.strip()!r}")
    if match is None:
        raise SelftestFormatError("no 'GECKO_CA ' line in self-test output")
    return Selftest(
        path=match.group("path"),
        exists=match.group("exists") == "1",
        nbytes=int(match.group("bytes")),
        frozen=match.group("frozen") == "1",
    )


def _denormalize_private(path: str) -> str:
    """Drop a leading macOS ``/private`` (so /private/etc -> /etc, /private/var -> /var)."""
    return path[len("/private") :] if path.startswith("/private/") else path


def is_system_store(path: str) -> bool:
    """True if ``path`` resolves to a system/keychain trust store (not the bundled cert)."""
    if "keychain" in path.lower():
        return True
    norm = _denormalize_private(path)
    return any(norm == p or norm.startswith(p + "/") for p in SYSTEM_PREFIXES)


def check(t: Selftest) -> list[str]:
    """Return every failure reason (empty list == the bundled cert is proven).

    All checks run so a failing CI log shows the whole story, not just the first fault.
    """
    reasons: list[str] = []
    if not t.frozen:
        reasons.append(
            "frozen=0 — not the frozen binary (self-test ran under a plain interpreter)"
        )
    if not t.path:
        reasons.append("path is empty — certifi.where() resolved to nothing")
    if not t.exists:
        reasons.append(f"exists=0 — the bundled cert is not on disk at {t.path!r}")
    if t.nbytes <= MIN_CA_BYTES:
        reasons.append(
            f"bytes={t.nbytes} <= {MIN_CA_BYTES} — empty/truncated CA bundle "
            "(a real cacert.pem is ~200 KB)"
        )
    if t.path and MEI_MARKER not in t.path:
        reasons.append(
            f"path {t.path!r} is not inside the PyInstaller _MEIPASS extraction dir "
            f"(no {MEI_MARKER!r}) — cannot prove it is the BUNDLED cert"
        )
    if is_system_store(t.path):
        reasons.append(
            f"path {t.path!r} resolves to a SYSTEM/keychain trust store, "
            "not the bundled cert — proves nothing about what shipped"
        )
    return reasons


def _read_input(argv: list[str]) -> str:
    if len(argv) > 1:
        with open(argv[1], encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return sys.stdin.read()


def main(argv: list[str]) -> int:
    try:
        t = parse_line(_read_input(argv))
    except SelftestFormatError as exc:
        print(f"::error::CA self-test: {exc}")
        return 1
    print(
        f"CA self-test line: path={t.path} exists={int(t.exists)} "
        f"bytes={t.nbytes} frozen={int(t.frozen)}"
    )
    reasons = check(t)
    if reasons:
        for reason in reasons:
            print(f"::error::CA self-test FAILED: {reason}")
        return 1
    print(
        "CA self-test PASSED: the binary shipped + resolved the bundled certifi "
        "(inside _MEIPASS, not a system store)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
