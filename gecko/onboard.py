"""`gecko add` onboarding — glue over the engine. Thin, control-plane only."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import docs_reader
from .netguard import Resolver, UnsafeUrlError, safe_get, validate_public_url

Fetcher = Callable[[str], str]


class OnboardError(Exception):
    """A recoverable onboarding failure (bad spec, unreachable source, etc.)."""


def _default_fetch(url: str) -> str:
    # SSRF-safe: validates every redirect hop, pins the socket, caps size/timeout —
    # the same machinery docs_reader already relies on for untrusted spec URLs.
    return safe_get(url)


@dataclass(frozen=True)
class ResolvedRef:
    """A resolved spec plus its trusted provenance (if any).

    ``spec_url`` is set ONLY when ``ref`` was an http(s) URL that yielded a direct JSON
    OpenAPI document — that's out-of-band provenance ``surfaces.anchor_for`` can pin to.
    It is ``None`` for docs-recovery (the parser guessed, so nothing is trusted yet) and
    for local paths (a file on disk is no more trustworthy than an in-memory dict — see
    ``anchor_for``'s docstring). Callers reconcile this into a ``base_url`` via
    ``pin_base_url``; never pin directly off the spec's own ``servers[]``.
    """

    spec: dict[str, Any]
    spec_url: str | None


def resolve_spec(
    ref: str, *, fetch: Fetcher | None = None, resolver: Resolver | None = None
) -> ResolvedRef:
    """Resolve an API reference to an OpenAPI dict + its trusted provenance.

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
                return ResolvedRef(spec=spec, spec_url=ref)
        except json.JSONDecodeError:
            pass
        # The exact ref wasn't a spec. Auto-discover: probe common spec locations on the
        # host (so `gecko add <domain>` just works), THEN fall back to docs recovery on the
        # original page. Docs recovery has no provenance — the parser guessed.
        discovered = discover_spec(ref, fetch=fetch, resolver=resolver)
        if discovered is not None:
            return discovered
        result = docs_reader.from_docs(ref)
        return ResolvedRef(spec=result.draft, spec_url=None)
    # Local path (dev convenience) — never pinning provenance.
    try:
        with open(ref, encoding="utf-8") as fh:
            return ResolvedRef(spec=json.load(fh), spec_url=None)
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardError(f"could not read spec at {ref}: {exc}") from exc


#: Common locations an OpenAPI spec is served under, probed in order when the ref itself
#: isn't a spec — turns `gecko add <domain>` into a one-liner that finds the surface.
_COMMON_SPEC_PATHS: tuple[str, ...] = (
    "/openapi.json",
    "/swagger.json",
    "/v1/openapi.json",
    "/api/openapi.json",
    "/api-docs",
    "/swagger/v1/swagger.json",
    "/.well-known/openapi.json",
    "/openapi",
)


def discover_spec(
    ref: str, *, fetch: Fetcher, resolver: Resolver | None = None
) -> ResolvedRef | None:
    """Probe common OpenAPI locations on ``ref``'s host; return the first that fetches
    and parses as an OpenAPI dict, else ``None``.

    Each probe is SSRF-validated independently (``validate_public_url`` + the socket-
    pinning ``fetch``). Best-effort: a probe that 404s, times out, is blocked, or returns
    non-JSON simply advances to the next path — it never raises. This is what lets a dev
    point ``gecko add`` at a bare domain and have Gecko locate the spec for them.
    """
    parsed = urlsplit(ref)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}"
    for path in _COMMON_SPEC_PATHS:
        url = base + path
        if url == ref:
            continue  # the exact ref was already tried by the caller
        try:
            validate_public_url(url, resolver=resolver)
            spec = json.loads(fetch(url))
        except (ValueError, OSError):
            # ValueError covers UnsafeUrlError (SSRF) + json.JSONDecodeError; OSError
            # covers fetch/HTTP failures. Any of them -> try the next candidate path.
            continue
        if isinstance(spec, dict) and spec.get("openapi"):
            return ResolvedRef(spec=spec, spec_url=url)
    return None


def pin_base_url(
    spec_url: str | None, spec: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Reconcile the fetch origin (trusted provenance) against the spec's own
    ``servers[0].url`` (untrusted — attacker-controlled, the confirmed exfil vector).

    Returns ``(base_url, warning)``:
      * ``spec_url`` is ``None`` (docs-recovery / local path) -> ``(None, None)``. Stay
        unverified — that's CORRECT, there is nothing to pin.
      * the spec's first server is relative or same-host absolute -> extract its path,
        force scheme/host/port from the trusted origin (defense against downgrade/port-
        substitution attacks), and return origin + path with no warning.
      * anything else (host mismatch, or no ``servers[]`` at all) -> pin to the bare
        fetch ORIGIN, never the spec's claim, with a warning a path prefix may be lost.
    """
    if spec_url is None:
        return None, None
    parsed = urlsplit(spec_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    spec_host = parsed.hostname
    servers = spec.get("servers") or []
    first = servers[0].get("url") if servers and isinstance(servers[0], dict) else None
    if first is None:
        warning = (
            f"warning: the spec's server host (None) differs from where the spec "
            f"was fetched ({spec_host}); pinning requests to the fetch origin — a path "
            "prefix may be lost."
        )
        return origin, warning
    fu = urlsplit(first)
    # Relative server URL (e.g., "/v1" or "") or same-host absolute: keep path, force
    # scheme/host/port from trusted origin.
    if fu.hostname is None or (spec_host and fu.hostname.lower() == spec_host.lower()):
        # Reconstruct: origin (trusted scheme/host/port) + path (from spec).
        base_url = origin + (fu.path or "")
        return base_url, None
    # Different host: reject the spec's claim, pin to origin with warning.
    warning = (
        f"warning: the spec's server host ({fu.hostname}) differs from where the spec "
        f"was fetched ({spec_host}); pinning requests to the fetch origin — a path "
        "prefix may be lost."
    )
    return origin, warning


def safe_name(ref: str) -> str:
    """A filesystem/name-safe surface id derived from a ref (host or slug)."""
    base = ref
    if ref.startswith(("http://", "https://")):
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
    base_url: str | None = None,
    mode: str = "recorded",
) -> ConfigResult:
    """Register the surface with Claude Code over stdio (client spawns the server).

    ``auth_surface``, when given, appends ``--auth-keychain <auth_surface>`` to the
    spawned ``gecko serve`` command — the whole point of sealing the key via
    ``ensure_key`` is that the SERVED surface resolves it at call time, not just
    that it sits unused in the OS keychain.

    ``base_url``, when given (from ``pin_base_url`` — the fetch origin, NEVER the
    spec's own ``servers[]``), appends ``--base-url <base_url>`` so the served surface
    is PINNED (see ``surfaces.anchor_for``) and can inject auth in live mode — a
    gecko-add-wired surface otherwise serves from a local cache path, which is not
    pinning provenance and stays ``unverified``.
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
    ]
    if base_url:
        command += ["--base-url", base_url]
    command += ["--stdio"]
    if auth_surface:
        command += ["--auth-keychain", auth_surface]
    # Recorded is serve's default ($0, synthesized) — only spell out the live
    # opt-in, so a recorded wiring stays byte-identical to before this flag existed.
    if mode == "live":
        command += ["--mode", "live"]
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


def add(
    ref: str,
    *,
    name: str | None = None,
    base_url: str | None = None,
    mode: str = "recorded",
    deps: AddDeps,
) -> int:
    """Comprehend `ref`, cache the surface, seal any key, and wire it into Claude.

    ``base_url`` (from `gecko add --base-url`) is an explicit, dev-asserted trusted
    host that OVERRIDES the fetch-origin pin — the one-line path for a spec whose own
    host doesn't serve it (e.g. Colosseum, whose OpenAPI lives off-host). It is
    SSRF-validated like any other request target, and it suppresses the host-mismatch
    warning because the dev is deliberately asserting the host.
    """
    if base_url is not None:
        try:
            validate_public_url(base_url, resolver=deps.resolver)
        except UnsafeUrlError as exc:
            print(f"  ✗ refusing unsafe --base-url: {exc}", file=sys.stderr)
            return 2
    try:
        resolved = resolve_spec(ref, fetch=deps.fetch, resolver=deps.resolver)
    except OnboardError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    spec = resolved.spec
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
    if base_url is not None:
        # Explicit dev-asserted host wins outright (already SSRF-validated above).
        pinned: str | None = base_url
        print(f"  ✓ request host pinned → {pinned}")
    else:
        # Reconcile the trusted fetch origin (never the spec's own servers[]) into an
        # explicit base_url so the SERVED surface pins (surfaces.anchor_for) instead of
        # degrading to unverified — the whole point: a cached-path surface can't pin on
        # its own, but the fetch-time provenance we already validated it against can.
        pinned, warn = pin_base_url(resolved.spec_url, spec)
        if warn:
            print(f"  ⚠ {warn}", file=sys.stderr)
    cfg = configure_claude(
        surface,
        path,
        run=deps.run,
        auth_surface=surface if needs_auth else None,
        base_url=pinned,
        mode=mode,
    )
    mark = "✓" if cfg.applied else "→"
    print(f"  {mark} {cfg.note}")
    if not cfg.applied:
        print("     " + " ".join(cfg.command))
    print(f"\n  → ask your agent to use the '{surface}' tools.")
    return 0
