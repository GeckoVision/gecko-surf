"""`gecko add` onboarding — glue over the engine. Thin, control-plane only."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import docs_reader
from .netguard import Resolver, UnsafeUrlError, safe_get, validate_public_url

Fetcher = Callable[[str], str]


class OnboardError(Exception):
    """A recoverable onboarding failure (bad spec, unreachable source, etc.)."""


def _default_fetch(url: str) -> str:
    # SSRF-safe: validates every redirect hop, pins the socket, caps size/timeout —
    # the same machinery docs_reader already relies on for untrusted spec URLs.
    return safe_get(url)


def resolve_spec(
    ref: str, *, fetch: Fetcher | None = None, resolver: Resolver | None = None
) -> dict[str, Any]:
    """Resolve an API reference to an OpenAPI dict.

    ``ref`` may be an http(s) OpenAPI URL, an http(s) docs page (recovered via
    from-docs), or a local path (dev). http(s) inputs are SSRF-validated first.
    """
    fetch = fetch or _default_fetch
    if ref.startswith(("http://", "https://")):
        try:
            validate_public_url(ref, resolver=resolver)
        except UnsafeUrlError as exc:
            raise OnboardError(f"refusing unsafe URL: {exc}") from exc
        body = fetch(ref)
        try:
            spec = json.loads(body)
            if isinstance(spec, dict) and spec.get("openapi"):
                return spec
        except json.JSONDecodeError:
            pass
        # Not a JSON spec — try docs recovery.
        result = docs_reader.from_docs(ref)
        return result.draft
    # Local path (dev convenience).
    try:
        with open(ref, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardError(f"could not read spec at {ref}: {exc}") from exc


def safe_name(ref: str) -> str:
    """A filesystem/name-safe surface id derived from a ref (host or slug)."""
    base = ref
    if ref.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        base = urlsplit(ref).netloc or ref
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return slug or "surface"


def cache_spec(name: str, spec: dict[str, Any], *, home: Path | None = None) -> Path:
    """Persist the comprehended spec (surface metadata only — no payloads)."""
    root = (home or Path.home()) / ".gecko" / "surfaces"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_name(name)}.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


Runner = Callable[[list[str]], int]


@dataclass(frozen=True)
class ConfigResult:
    ok: bool
    command: list[str]
    applied: bool
    note: str


def _default_run(cmd: list[str]) -> int:
    import subprocess

    return subprocess.run(cmd, check=False).returncode


def spec_needs_auth(spec: dict[str, Any]) -> bool:
    """True if the spec declares any security scheme (so the API needs a key)."""
    schemes = spec.get("components", {}).get("securitySchemes")
    return bool(schemes) or bool(spec.get("security"))


def ensure_key(
    name: str,
    *,
    prompt: Callable[[str], str],
    store: Callable[[str, str], bool],
) -> bool:
    """Prompt (hidden, injected) for the provider key and store it. Never logged.

    Returns True only if a secret was entered AND actually persisted — a
    degraded/unavailable keychain must not be reported as success.
    """
    secret = prompt(f"Enter API key for {name} (hidden, stored in OS keychain): ")
    if not secret:
        return False
    return store(name, secret)


def configure_claude(
    name: str,
    cache_path: Path,
    *,
    gecko_bin: str = "gecko",
    run: Runner | None = None,
    auth_surface: str | None = None,
) -> ConfigResult:
    """Register the surface with Claude Code over stdio (client spawns the server).

    ``auth_surface``, when given, appends ``--auth-keychain <auth_surface>`` to the
    spawned ``gecko serve`` command — the whole point of sealing the key via
    ``ensure_key`` is that the SERVED surface resolves it at call time, not just
    that it sits unused in the OS keychain.
    """
    run = run or _default_run
    command = [
        "claude",
        "mcp",
        "add",
        "--transport",
        "stdio",
        name,
        "--",
        gecko_bin,
        "serve",
        str(cache_path),
        "--stdio",
    ]
    if auth_surface:
        command += ["--auth-keychain", auth_surface]
    try:
        code = run(command)
    except FileNotFoundError:
        return ConfigResult(
            True,
            command,
            False,
            "Claude Code CLI not found — run the command above yourself.",
        )
    if code == 0:
        return ConfigResult(True, command, True, "added to Claude Code (stdio).")
    return ConfigResult(
        True,
        command,
        False,
        f"`claude mcp add` exited {code} — run the command above yourself.",
    )


@dataclass
class AddDeps:
    """Injected dependencies for ``add`` — the seam that keeps it network-free in tests."""

    fetch: Fetcher
    comprehend: Callable[[dict[str, Any]], int]
    prompt: Callable[[str], str]
    store: Callable[[str, str], bool]
    run: Runner
    home: Path
    resolver: Resolver | None = None


def list_surfaces(*, home: Path) -> list[str]:
    """List all onboarded surface names (sorted stems under ~/.gecko/surfaces/)."""
    root = home / ".gecko" / "surfaces"
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.json"))


def remove(name: str, *, run: Runner, home: Path) -> int:
    """Deregister and delete a cached surface.

    Runs `claude mcp remove <safe_name>` (tolerating FileNotFoundError if the client
    is absent), then unlinks the cache file (missing_ok=True).
    Returns 0 on success.
    """
    slug = safe_name(name)
    try:
        run(["claude", "mcp", "remove", slug])
    except FileNotFoundError:
        pass  # client not present; still drop the cache
    path = home / ".gecko" / "surfaces" / f"{slug}.json"
    path.unlink(missing_ok=True)
    print(f"  removed surface '{slug}'")
    return 0


def add(ref: str, *, name: str | None = None, deps: AddDeps) -> int:
    """Comprehend `ref`, cache the surface, seal any key, and wire it into Claude."""
    try:
        spec = resolve_spec(ref, fetch=deps.fetch, resolver=deps.resolver)
    except OnboardError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    # Slugify ONCE, here — cache_spec/remove re-slug via safe_name too, so a raw
    # `--name "My API"` must not desync the Claude registration from the cache
    # file / credential slot those look up by slug.
    surface = safe_name(name) if name else safe_name(ref)
    n_tools = deps.comprehend(spec)
    print(f"  ✓ comprehended {n_tools} endpoint(s) → first-call-correct tools")
    needs_auth = spec_needs_auth(spec)
    if needs_auth:
        if ensure_key(surface, prompt=deps.prompt, store=deps.store):
            print("  ✓ key → sealed in OS keychain (never in mcp.json)")
        else:
            print(
                "  ○ no key entered — add later with `gecko auth set " + surface + "`"
            )
    path = cache_spec(surface, spec, home=deps.home)
    cfg = configure_claude(
        surface, path, run=deps.run, auth_surface=surface if needs_auth else None
    )
    mark = "✓" if cfg.applied else "→"
    print(f"  {mark} {cfg.note}")
    if not cfg.applied:
        print("     " + " ".join(cfg.command))
    print(f"\n  → ask your agent to use the '{surface}' tools.")
    return 0
