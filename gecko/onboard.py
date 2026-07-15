"""`gecko add` onboarding — glue over the engine. Thin, control-plane only."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__, docs_reader
from .client import ambiguous_server_message
from .netguard import Resolver, UnsafeUrlError, safe_get, validate_public_url
from .telemetry import telemetry_enabled

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


#: Any URI scheme (``https://``, ``ftp://``, ``file://`` …) — the classifier that
#: separates "URL-shaped" from "a bare domain the dev typed" (``api.example.com``).
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def resolve_spec(
    ref: str, *, fetch: Fetcher | None = None, resolver: Resolver | None = None
) -> ResolvedRef:
    """Resolve an API reference to an OpenAPI dict + its trusted provenance.

    ``ref`` may be an http(s) OpenAPI URL, an http(s) docs page (recovered via
    from-docs), a local path (dev), or a schemeless bare domain
    (``gecko add api.example.com``) — retried as ``https://<ref>`` through the same
    pipeline. http(s) inputs are SSRF-validated first; an existing local file always
    wins over the https interpretation (no network for something on disk).
    """
    fetch = fetch or _default_fetch
    if ref.startswith(("http://", "https://")):
        return _resolve_url(ref, fetch=fetch, resolver=resolver)
    if not _SCHEME_RE.match(ref) and not os.path.exists(ref):
        # A bare domain (the Pegana field repro): nothing on disk to read, so retry
        # as https through the SAME SSRF-validated URL/discovery pipeline. When that
        # also fails, name BOTH interpretations — a bare ENOENT for a domain-shaped
        # ref is what confused the field.
        candidate = f"https://{ref}"
        try:
            return _resolve_url(candidate, fetch=fetch, resolver=resolver)
        except (OnboardError, OSError, ValueError) as exc:
            raise OnboardError(
                f"could not resolve {ref!r}: no such local file, and trying it as "
                f"{candidate} failed: {exc}"
            ) from exc
    # Local path (dev convenience) — never pinning provenance. Non-http(s) schemes
    # also land here (unchanged): they never had URL handling.
    try:
        with open(ref, encoding="utf-8") as fh:
            return ResolvedRef(spec=json.load(fh), spec_url=None)
    except (OSError, json.JSONDecodeError) as exc:
        raise OnboardError(f"could not read spec at {ref}: {exc}") from exc


def _resolve_url(ref: str, *, fetch: Fetcher, resolver: Resolver | None) -> ResolvedRef:
    """The http(s) pipeline: SSRF-validate, fetch, then spec → discovery → docs."""
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


#: The npm package that ships the frozen ``gecko`` binary — what ``npx -y`` re-resolves
#: each spawn, so a wired server survives the npx cache being pruned.
_NPX_PACKAGE = "@geckovision/gecko"


def _serve_launcher() -> list[str]:
    """The argv prefix that re-launches THIS gecko for a wired MCP server.

    ``claude mcp add`` registers a command the client spawns LATER, from a fresh
    shell — so it must survive the way gecko was invoked NOW. Three worlds:

    * npx (``npx @geckovision/gecko add …``): the PyInstaller binary runs out of the
      npm/npx cache (path contains ``_npx`` or ``.npm``) — a path npx prunes at will,
      and ``gecko`` is NOT on PATH. Register ``npx -y <pkg>`` so the client
      re-resolves the package on every spawn.
    * a frozen binary at a stable path (real install): ``gecko`` may still not be on
      the client's PATH — register the absolute executable path.
    * pip/uvx (not frozen): ``gecko`` IS a console script on PATH — keep it.
    """
    if not getattr(sys, "frozen", False):  # PyInstaller sets sys.frozen = True
        return ["gecko"]
    executable = sys.executable or ""
    if "_npx" in executable or ".npm" in executable:
        return ["npx", "-y", _NPX_PACKAGE]
    return [executable or "gecko"]


def configure_claude(
    name: str,
    cache_path: Path,
    *,
    gecko_bin: str | None = None,
    run: Runner | None = None,
    auth_surface: str | None = None,
    base_url: str | None = None,
    mode: str = "recorded",
) -> ConfigResult:
    """Register the surface with Claude Code over stdio (client spawns the server).

    The spawned command's launcher comes from ``_serve_launcher()`` — bare ``gecko``
    only when a console script is actually on PATH, ``npx -y``/an absolute path for
    the frozen-binary worlds. ``gecko_bin`` (when given) overrides it outright.

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
    launcher = [gecko_bin] if gecko_bin is not None else _serve_launcher()
    command = [
        "claude",
        "mcp",
        "add",
        "--transport",
        "stdio",
        name,
        "--",
        *launcher,
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


# --------------------------------------------------------------------------- #
# The onboard ping — the attribution event that makes `gecko add` adopters visible.
# Default-on, aggregate-only, opt-out (GECKO_TELEMETRY=off). Control plane by
# construction: five short labels — API host, CLI version, OS family, a random
# install id, the mode. Never an arg, payload, key, path, or email.
# --------------------------------------------------------------------------- #
#: Where the ping lands (the hosted server's POST /events/onboard ingest).
ONBOARD_PING_URL = "https://mcp.geckovision.tech/events/onboard"
#: Printed when (and only when) a ping actually left the machine. Non-negotiable:
#: a default-on ping the user cannot see would be spyware.
ONBOARD_PING_NOTE = (
    "  · anonymous onboard ping (host, version, os — GECKO_TELEMETRY=off to disable)"
)
_PING_TIMEOUT_S = 2.0
_PING_MAX_VALUE = 64  # mirror of the server's per-value cap

#: The injected POST seam (mirrors ``login.Post``): tests record it, the CLI wires
#: ``_default_ping_post``. ``AddDeps.ping_post is None`` (the library default) sends
#: NOTHING — an embedded ``onboard.add`` stays network-silent.
PingPost = Callable[[str, dict[str, str]], None]


def _default_ping_post(url: str, payload: dict[str, str]) -> None:
    """SSRF-validated fire-and-forget JSON POST (stdlib urllib, 2s hard cap)."""
    validate_public_url(url)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=_PING_TIMEOUT_S) as resp:  # noqa: S310
        resp.read()


def read_or_create_install_id(home: Path) -> str:
    """The opaque install id: a RANDOM ``uuid4().hex`` persisted once at
    ``<home>/.gecko/install_id`` (0600).

    NOT user-derived — no hostname, email, or machine id goes in — so it counts an
    install, never identifies a person. Best-effort: an unreadable or unwritable
    file degrades to an ephemeral id rather than an error."""
    path = home / ".gecko" / "install_id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    new_id = uuid.uuid4().hex
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # os.open sets 0600 atomically at creation (hygiene; the id is not a secret).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_id + "\n")
    except OSError:
        pass  # ephemeral this run; the adopter still counts once
    return new_id


def _client_os() -> str:
    """``sys.platform`` normalized to a coarse OS family — a label, not a fingerprint."""
    platform = sys.platform
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "darwin"
    if platform.startswith(("win", "cygwin")):
        return "windows"
    return platform[:_PING_MAX_VALUE]


def _ping_host(ref: str, base_url: str | None) -> str:
    """The HOST an add is attributed to — never a path, query, credential, or local
    file path. An http(s) ref wins; else an explicit ``--base-url``; a purely local
    add is the literal ``"local"`` (a filesystem path could carry a username — never
    send one)."""
    for candidate in (ref, base_url or ""):
        if candidate.startswith(("http://", "https://")):
            host = urlsplit(candidate).hostname
            if host:
                return host[:_PING_MAX_VALUE]
    return "local"


def send_onboard_ping(
    *,
    ref: str,
    base_url: str | None,
    mode: str,
    home: Path,
    post: PingPost,
    url: str = ONBOARD_PING_URL,
) -> None:
    """Fire the anonymous onboard ping and print the transparency line.

    ``GECKO_TELEMETRY=off`` disables it — then NOTHING is sent or printed. Swallows
    everything (a dead network, slow DNS under the 2s cap, even a bug here): the
    ping must never break or noticeably slow ``gecko add``. The note prints only
    AFTER a POST actually went out — never claim a send that failed."""
    try:
        if not telemetry_enabled():
            return
        payload = {
            "surface_host": _ping_host(ref, base_url),
            "version": str(__version__)[:_PING_MAX_VALUE],
            "client_os": _client_os(),
            "install_id": read_or_create_install_id(home),
            # Fold anything unexpected to the $0 default — the wire set is closed.
            "mode": mode if mode == "live" else "recorded",
        }
        post(url, payload)
    except Exception:  # noqa: BLE001 - fire-and-forget; the ping never breaks `add`
        return
    print(ONBOARD_PING_NOTE)


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
    #: The onboard-ping POST. ``None`` (the library default) sends NOTHING; only the
    #: CLI wires the real transport — so `gecko add` is default-on (opt-out via
    #: GECKO_TELEMETRY=off) while embedded/library use stays network-silent.
    ping_post: PingPost | None = None


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
    servers = spec.get("servers") or []
    if mode == "live" and base_url is None and len(servers) > 1:
        # The same fail-closed the live-call seam enforces (client.AmbiguousServerError),
        # surfaced at onboard time — BEFORE the key prompt, cache write, or Claude wiring
        # — so a live surface is never wired to raise on its very first call.
        print(f"  ✗ {ambiguous_server_message(servers)}", file=sys.stderr)
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
    if deps.ping_post is not None:
        # The adopter becomes visible — fires only HERE, after the surface actually
        # wired. Default-on with the transparency line; GECKO_TELEMETRY=off opts out.
        send_onboard_ping(
            ref=ref, base_url=base_url, mode=mode, home=deps.home, post=deps.ping_post
        )
    print(f"\n  → ask your agent to use the '{surface}' tools.")
    return 0
