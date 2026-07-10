"""``gecko doctor`` — read-only self-diagnosis of the local MCP setup.

Point it at the environment (optionally a surface name) and it prints the *exact*
command to run to add the server — human-readable by default, ``--json`` for an
agent to consume and act on (the Component-4 hook). It DIAGNOSES; it never edits the
user's MCP config, never writes files, and never touches the network.

Determinism + safety:
- Every check is offline and side-effect-free: ``mcp`` importability (``find_spec``),
  a credential PRESENCE probe (never the value), and — for the remote/HTTP path only —
  ``cloudflared`` on ``PATH`` (``shutil.which``).
- No secret ever reaches a field, a repr, a log, or the JSON. The credential check
  reports only which backend WOULD answer, via ``credentials.which_backend``.
- stdio is the recommended transport (no port, no tunnel); HTTP is recommended only
  when the caller declares a genuinely remote/sandboxed client (``remote=True``).
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import asdict, dataclass, field
from typing import Any

from . import credentials, deeplinks

# The packaged stdio spawn form the serve banner also emits — one source of truth for
# "how a client launches this surface over stdio". ``<name>-mcp`` is the console entry.
_PACKAGED_SPAWN = 'uvx --from "gecko-surf[serve]" {name}-mcp --stdio'
_MCP_PATH = "/mcp"

# Non-secret remediation strings (safe to log / emit).
_SERVE_INSTALL_HINT = (
    "install the serve extra to enable stdio/HTTP: uv pip install 'gecko-surf[serve]'"
)
_CLOUDFLARED_HINT = (
    "cloudflared not on PATH — install it for the --tunnel fallback, or use stdio "
    "(no tunnel needed)"
)


@dataclass(frozen=True)
class Check:
    """One diagnosis line. ``detail`` is always safe to print — never a secret."""

    name: str
    ok: bool
    detail: str


@dataclass
class DoctorReport:
    """The full diagnosis. Every field is control-plane only — no secret, ever."""

    checks: list[Check]
    recommended_transport: str  # "stdio" | "http"
    add_command: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready projection so an agent can read the report and act on it."""
        return {
            "checks": [asdict(c) for c in self.checks],
            "recommended_transport": self.recommended_transport,
            "add_command": self.add_command,
            "warnings": list(self.warnings),
        }


def _mcp_available() -> bool:
    """Is the ``mcp`` SDK importable (the ``serve`` extra)? Offline, no import side
    effects — ``find_spec`` only locates it. A broken parent package reads as absent."""
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def _mcp_check() -> Check:
    if _mcp_available():
        return Check(
            name="mcp",
            ok=True,
            detail="the mcp serve extra is installed (stdio/HTTP transport available).",
        )
    return Check(name="mcp", ok=False, detail=_SERVE_INSTALL_HINT)


def _credential_check(api: str, resolver: credentials.ChainResolver) -> Check:
    """Presence-probe the credential for ``api``. Reports ONLY which backend would
    answer — never the value, its length, or a prefix. A configured command that
    fails is treated as unresolved (its redacted error is swallowed here)."""
    ref = credentials.CredentialRef(api=api)
    try:
        backend = credentials.which_backend(ref, resolver)
    except credentials.CredentialError:
        # A fetch command exists but failed — a miss for diagnosis; stay redacted.
        backend = None
    if backend is not None:
        return Check(
            name=f"credential:{api}",
            ok=True,
            detail=f"a credential is present for '{api}' (via {backend}).",
        )
    return Check(
        name=f"credential:{api}",
        ok=False,
        detail=f"no credential for '{api}' — run: gecko auth set {api}",
    )


def _cloudflared_check() -> Check:
    """HTTP/remote only: is ``cloudflared`` on PATH for the ``--tunnel`` fallback?"""
    if shutil.which("cloudflared") is not None:
        return Check(
            name="cloudflared",
            ok=True,
            detail="cloudflared is on PATH (the --tunnel fallback is available).",
        )
    return Check(name="cloudflared", ok=False, detail=_CLOUDFLARED_HINT)


def _stdio_add_command(name: str) -> str:
    """The exact ``claude mcp add <name> -- <spawn> --stdio`` line (reuse deeplinks)."""
    return deeplinks.claude_stdio_add_command(name, _PACKAGED_SPAWN.format(name=name))


def _http_add_command(name: str) -> str:
    """The HTTP add line. doctor doesn't serve, so the public URL is a placeholder the
    caller fills from ``gecko serve <spec> --http --tunnel`` (Component 3)."""
    return deeplinks.claude_add_command(name, f"https://<public-url>{_MCP_PATH}")


def run_doctor(
    api: str | None = None,
    *,
    remote: bool = False,
    resolver: credentials.ChainResolver | None = None,
) -> DoctorReport:
    """Diagnose the local MCP setup and emit the exact add command. Read-only: no
    config edit, no file write, no network. ``resolver`` is injectable for tests;
    it defaults to the standard keyring -> command -> env chain.

    stdio is recommended by default (no port, no tunnel). HTTP is recommended only
    for a genuinely remote/sandboxed client (``remote=True``), where the tunnel
    fallback matters — so ``cloudflared`` is probed in that mode only.
    """
    checks: list[Check] = []
    warnings: list[str] = []

    mcp_check = _mcp_check()
    checks.append(mcp_check)
    if not mcp_check.ok:
        warnings.append(_SERVE_INSTALL_HINT)

    if api is not None:
        cred_check = _credential_check(api, resolver or credentials.default_resolver())
        checks.append(cred_check)
        if not cred_check.ok:
            warnings.append(f"no credential for '{api}' — run: gecko auth set {api}")

    name = api or "<name>"
    if remote:
        transport = "http"
        cf_check = _cloudflared_check()
        checks.append(cf_check)
        if not cf_check.ok:
            warnings.append(_CLOUDFLARED_HINT)
        warnings.append(
            "remote/HTTP needs a public URL — get one with: "
            "gecko serve <spec> --http --tunnel, then use it in the add command."
        )
        add_command = _http_add_command(name)
    else:
        transport = "stdio"
        add_command = _stdio_add_command(name)

    return DoctorReport(
        checks=checks,
        recommended_transport=transport,
        add_command=add_command,
        warnings=warnings,
    )


def render_text(report: DoctorReport) -> str:
    """Human-readable table: a ✓/✗ per check, then the one command to run. Pure
    formatting (kept here so the CLI stays thin and this output is testable)."""
    lines = ["gecko doctor — MCP setup diagnosis", "=" * 40]
    for check in report.checks:
        mark = "✓" if check.ok else "✗"
        lines.append(f"  {mark} {check.name}: {check.detail}")
    lines.append("")
    lines.append(f"recommended transport: {report.recommended_transport}")
    lines.append("run this to add the server:")
    lines.append(f"  {report.add_command}")
    if report.warnings:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"  - {w}" for w in report.warnings)
    return "\n".join(lines)
