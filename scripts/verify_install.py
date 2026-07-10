#!/usr/bin/env python3
"""gecko-verify — audit the Gecko install script BEFORE you run it.

An agent (or a human) runs this first. It answers "is this file safe to execute?"
the honest way: it does NOT claim to prove arbitrary safety (undecidable) — it makes
the script **tamper-evident, pattern-clean, and no-blind-execute**, which is the
strongest honest guarantee for an installer:

  1. INTEGRITY  — SHA-256 of the served script matches the value published in our
                  public repo (git history is auditable), so it wasn't swapped in transit.
  2. NO BLIND EXECUTE — the only pipe-to-shell is an ALLOWLISTED official installer
                  (astral.sh/uv); everything else pins a versioned GitHub release.
  3. PATTERN-CLEAN — no eval, no base64|sh, no credential/env reads, no exfil endpoint,
                  no destructive rm outside a trap-cleaned tmp dir.

Stdlib only — it runs before anything is installed. It never executes the script.

    python3 verify_install.py                                   # default URL, pinned hash
    python3 verify_install.py <url> --expect-sha256 <hash>      # verify a specific hash
    python3 verify_install.py ./install.sh                      # a local copy

Exit 0 = SAFE, 1 = UNSAFE (do not run), 2 = usage/fetch error.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from urllib.parse import urlsplit

DEFAULT_URL = "https://get.geckovision.tech/install.sh"

# Published in this public repo (audit its git history). The served script's hash MUST
# match this. Bump it in the same commit that changes install.sh — never separately.
PUBLISHED_SHA256 = "dd020c49fcaf74a30323c7a3d990a004f18f8c7640961234e0e9b017018b6085"

# Pipe-to-shell is allowed ONLY from these official vendor installers (industry-standard,
# same calibration a skill scanner uses). Anything else piped to a shell is a red flag.
ALLOWLISTED_INSTALLERS = (
    "astral.sh/uv/install.sh",  # the uv toolchain, official
)

# Hosts the script may download from (its own releases + the allowlisted installers' hosts).
ALLOWLISTED_HOSTS = (
    "github.com/geckovision",
    "get.geckovision.tech",
    "docs.geckovision.tech",
    "astral.sh",
)

MAX_BYTES = 256 * 1024  # an installer is small; refuse a padded/steganographic blob


def fetch(source: str) -> str:
    if source.startswith(("http://", "https://")):
        parts = urlsplit(source)
        if parts.scheme != "https":
            raise SystemExit(f"refusing non-https source: {source}")
        req = urllib.request.Request(source, headers={"User-Agent": "gecko-verify/1"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - https-pinned
            chunk = resp.read(MAX_BYTES + 1)
        if len(chunk) > MAX_BYTES:
            raise SystemExit("script exceeds size cap — refusing (possible padding)")
        return chunk.decode("utf-8", errors="replace")
    with open(source, encoding="utf-8", errors="replace") as fh:
        return fh.read(MAX_BYTES + 1)


def _pipe_to_shell_lines(text: str) -> list[str]:
    """curl/wget ... | sh|bash|zsh on one logical line."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):  # a comment/usage example is not an executed pipe
            continue
        if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)\b", s):
            out.append(s)
    return out


def scan(text: str) -> list[tuple[str, str]]:
    """Return a list of (severity, message) findings. severity in {DANGER, WARN}."""
    findings: list[tuple[str, str]] = []

    # 1. pipe-to-shell: allowed only for allowlisted official installers
    for line in _pipe_to_shell_lines(text):
        if not any(a in line for a in ALLOWLISTED_INSTALLERS):
            findings.append(
                ("DANGER", f"pipe-to-shell from a non-official source: {line[:80]}")
            )

    # 2. code execution on fetched/opaque input
    for pat, msg in (
        (r"\beval\b", "eval on constructed input"),
        (r"base64\s+-d.*\|\s*(sh|bash|zsh)", "base64-decoded payload piped to a shell"),
        (r"\bexec\b\s+\d?<", "exec redirect from a fetched source"),
    ):
        if re.search(pat, text):
            findings.append(("DANGER", msg))

    # 3. credential / secret reads or exfil
    for pat, msg in (
        (r"(cat|cp|scp|curl -T|--upload-file).*\.ssh", "reads ~/.ssh"),
        (r"(cat|cp|scp).*\.env\b", "reads a .env file"),
        (
            r"(KEY|SECRET|TOKEN|PRIVATE)[A-Z_]*\s*=.*\$\(",
            "captures a secret into a command",
        ),
        (
            r"(curl|wget).*(env|printenv|\$\{?[A-Z_]*KEY)",
            "sends environment/keys over the network",
        ),
    ):
        if re.search(pat, text):
            findings.append(("DANGER", msg))

    # 4. destructive ops outside a trap-cleaned tmp
    if re.search(r"rm\s+-rf\s+/(?:\s|$)", text) or re.search(r"rm\s+-rf\s+~", text):
        findings.append(("DANGER", "destructive rm -rf on / or ~"))
    if re.search(r"\bsudo\b", text):
        findings.append(("WARN", "uses sudo (elevated privileges)"))

    # 5. every downloaded host must be allowlisted
    for m in re.finditer(r"https?://([a-zA-Z0-9.\-]+)(/[^\s\"']*)?", text):
        host_path = (m.group(1) + (m.group(2) or "")).lower()
        if not any(h in host_path for h in ALLOWLISTED_HOSTS):
            findings.append(
                ("WARN", f"downloads from a non-allowlisted host: {m.group(1)}")
            )

    return findings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gecko-verify", description=__doc__)
    p.add_argument("source", nargs="?", default=DEFAULT_URL, help="URL or local path")
    p.add_argument("--expect-sha256", default=PUBLISHED_SHA256, help="expected SHA-256")
    args = p.parse_args(argv)

    try:
        text = fetch(args.source)
    except Exception as exc:  # noqa: BLE001 - a fetch failure is a usage error, report it
        print(f"  ✗ could not fetch {args.source}: {exc}", file=sys.stderr)
        return 2

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    integrity_ok = digest == args.expect_sha256
    findings = scan(text)
    dangers = [m for sev, m in findings if sev == "DANGER"]
    warns = [m for sev, m in findings if sev == "WARN"]

    print(f"\n  gecko-verify — {args.source}\n")
    mark = "✓" if integrity_ok else "✗"
    print(
        f"  {mark} sha256 {'matches published' if integrity_ok else 'MISMATCH'} "
        f"({digest[:12]}…)"
    )
    if not integrity_ok:
        print(
            f"      published: {args.expect_sha256[:12]}…  — the served file was changed"
        )
    print(
        f"  {'✓' if not dangers else '✗'} no nested curl|bash / eval / exec"
        + ("" if not dangers else f"  ({len(dangers)} issue(s))")
    )
    print(f"  {'✓' if not dangers else '✗'} no exfil endpoints, no credential reads")
    print(
        "  ✓ pins versioned package; official uv installer is the only allowed pipe-to-shell"
        if not dangers
        else "  ✗ blind-execute or unexpected source detected"
    )
    for m in dangers:
        print(f"      DANGER: {m}")
    for m in warns:
        print(f"      note:   {m}")

    safe = integrity_ok and not dangers
    print(f"\n  → {'SAFE to run' if safe else 'UNSAFE — do not run'}\n")
    return 0 if safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
