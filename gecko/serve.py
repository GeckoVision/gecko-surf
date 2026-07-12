"""``python -m gecko.serve <openapi-url>`` — paste an API, serve it to agents.

The whole M1 distribution flow as a CLI: SSRF-validate the spec URL, comprehend it
with the unchanged engine, print the MCP URL + one-click add strings for each host
app, then serve the surface over Streamable HTTP.

Thin by design — every line of real logic lives in the package (netguard, ingest,
client, http_server, deeplinks). This module only parses args and formats output.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

from .access import keychain_session, public_session
from .client import AgentApiClient
from .credentials import CredentialError
from .deeplinks import all_add_strings, claude_stdio_add_command
from .http_server import MCP_PATH, serve_http
from .mcp_server import serve_stdio
from .netguard import UnsafeUrlError, validate_public_url


_DEFAULT_REGISTRY_URL = "https://mcp.geckovision.tech"


def _slugify(text: str, fallback: str = "gecko") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or fallback


def _summary(client: AgentApiClient) -> str:
    total = len(client.operations)
    usable = len(client.list_tools())
    hidden = len(client.tools) - usable
    return (
        f"comprehended {total} operations -> {usable} usable as tools "
        f"({hidden} auth-gated hidden from the agent)"
    )


def _mcp_url(host: str, port: int, public_url: str | None) -> str:
    if public_url:
        base = public_url.rstrip("/")
        return base if base.endswith(MCP_PATH) else base + MCP_PATH
    return f"http://{host}:{port}{MCP_PATH}"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m gecko.serve",
        description="Comprehend a public OpenAPI URL and serve it to agents over MCP.",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "spec",
        nargs="?",
        default=None,
        help="Public OpenAPI 3.x URL (or local path for dev).",
    )
    source.add_argument(
        "--registry",
        default=None,
        help="Fetch a comprehended surface from the Gecko registry by name "
        "(instead of a spec URL/path).",
    )
    p.add_argument(
        "--registry-url",
        default=_DEFAULT_REGISTRY_URL,
        help="Registry base URL.",
    )
    auth = p.add_mutually_exclusive_group()
    auth.add_argument(
        "--auth-env",
        default=None,
        help="Env var holding the PROVIDER bearer token — injected locally at "
        "call time, never sent to Gecko.",
    )
    auth.add_argument(
        "--auth-keychain",
        default=None,
        metavar="SURFACE",
        help="Surface name whose key was sealed via `gecko add` / `gecko auth "
        "set` — resolved from the OS keychain (or configured command/env "
        "fallback) at call time, with header/scheme derived from the spec's own "
        "security scheme. Never sent to Gecko.",
    )
    p.add_argument(
        "--stdio",
        action="store_true",
        help="Serve over stdio (the client SPAWNS this process; no port, no tunnel) "
        "instead of HTTP. The zero-friction local path — recommended for a single "
        "developer on one machine.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=8000, help="Bind port (default 8000).")
    p.add_argument(
        "--mode",
        choices=("recorded", "live"),
        default="recorded",
        help="recorded ($0, synthesized) or live (real upstream calls).",
    )
    p.add_argument(
        "--name", default=None, help="Server/tool name (default: spec slug)."
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Pin the trusted request host (the origin the spec was fetched from). "
        "Enables live auth injection.",
    )
    p.add_argument(
        "--public-url",
        default=None,
        help="Public HTTPS URL the agent will connect to (e.g. a tunnel). "
        "Advertised in the add strings and trusted for Host/Origin.",
    )
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Extra Host header to allow (repeatable; for a tunnel hostname).",
    )
    p.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="Extra Origin to allow (repeatable).",
    )
    p.add_argument(
        "--emit-dir",
        default=None,
        help="Write this API's agent-native discovery files (llms.txt, gecko.json, "
        ".well-known/gecko.json, tools.md) to DIR and exit — no server. Hand them to "
        "the provider to host so their API is discoverable to agents.",
    )
    p.add_argument(
        "--site-url",
        default=None,
        help="Base URL the emitted files will be hosted at (makes inter-file links "
        "absolute; relative when omitted).",
    )
    return p.parse_args(argv)


def _emit(
    client: AgentApiClient, out_dir: str, mcp_url: str | None, site_url: str | None
) -> int:
    """Write the agent-native artifacts to ``out_dir`` (control-plane only) and return."""
    from pathlib import Path

    from .agentnative import build_artifacts

    artifacts = build_artifacts(client, mcp_url=mcp_url, site_url=site_url)
    out = Path(out_dir)
    for rel, text in artifacts.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    print(
        f"Wrote {len(artifacts)} agent-native artifacts to {out}/ (control-plane only):"
    )
    for rel in artifacts:
        print(f"  {out / rel}")
    return 0


def _stdio_spawn(args: argparse.Namespace) -> str:
    """The exact command a client spawns to run THIS surface over stdio — the spawn
    target for the recommended ``claude mcp add <name> -- <spawn>`` line. Mirrors how
    the surface was selected (a spec URL/path vs a registry name)."""
    if args.registry:
        spawn = f"gecko --registry {args.registry}"
        if args.registry_url and args.registry_url != _DEFAULT_REGISTRY_URL:
            spawn += f" --registry-url {args.registry_url}"
    else:
        spawn = f"gecko {args.spec}"
    if args.auth_env:
        spawn += f" --auth-env {args.auth_env}"
    if args.auth_keychain:
        spawn += f" --auth-keychain {args.auth_keychain}"
    if args.base_url:
        spawn += f" --base-url {args.base_url}"
    return spawn + " --stdio"


def _print_banner(name: str, mcp_url: str, summary: str, stdio_spawn: str) -> None:
    print("Gecko — make any API agent-usable (gecko-surf)\n" + "=" * 56)
    print(summary)
    print("Control plane: Gecko stores only the API surface — never your data,")
    print("never response payloads, never secrets.\n")

    # Lead with stdio: the client spawns this process over stdin/stdout, so there is
    # NO port and NO tunnel — it sidesteps the "connected but 0 tools" localhost trap
    # entirely. This is the recommended path for a single developer on one machine.
    print(
        "Recommended — zero-friction stdio (no port, no tunnel; your agent spawns it):"
    )
    print(f"  {claude_stdio_add_command(name, stdio_spawn)}")
    packaged_spawn = f'uvx --from "gecko-surf[serve]" {name}-mcp --stdio'
    print(
        f"  published as a package?  {claude_stdio_add_command(name, packaged_spawn)}\n"
    )

    # HTTP is for the real remote/shared case only — demoted below the stdio default.
    print("Serving to a remote or shared client? Use the HTTP URL instead:")
    print(f"  MCP URL (Streamable HTTP):  {mcp_url}")
    adds = all_add_strings(name, mcp_url)
    print(f"  Claude Code:  {adds['claude']}")
    print(f"  Cursor:       {adds['cursor']}")
    print(f"  VS Code:      {adds['vscode']}\n")
    # Self-diagnose the #1 HTTP activation failure: the client reports "connected" but
    # loads ZERO tools. Give the cheap fixes FIRST (poll delay / stale registration),
    # then the real cause — a sandboxed/remote agent's MCP client runs in a different
    # network namespace than this shell and can't reach 127.0.0.1 — and only then the
    # public-URL/tunnel workaround (a real exposure, so it's the last resort).
    if mcp_url.startswith("http://127.0.0.1") or mcp_url.startswith("http://localhost"):
        print("Connected but your agent shows 0 tools?")
        print("  1. Wait ~20s for the client to poll, and re-open the tool list.")
        print(
            f"  2. Clear a stale registration:  claude mcp remove {name}  then re-add."
        )
        print(
            "  3. Prefer --stdio (above) — it removes the localhost problem entirely."
        )
        print(
            "  4. Sandboxed/remote agent that truly needs HTTP? Its MCP client runs in a"
        )
        print(
            "     separate network namespace and can't reach localhost — serve behind"
        )
        print("     a public URL:")
        print("       cloudflared tunnel --url " + mcp_url.rsplit("/mcp", 1)[0])
        print("       gecko <spec> --public-url https://<name>.trycloudflare.com\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if bool(args.spec) == bool(args.registry):
        print("Provide exactly one of <spec> or --registry <name>.", file=sys.stderr)
        return 2

    if args.base_url:
        try:
            validate_public_url(args.base_url)
        except UnsafeUrlError as exc:
            print(f"Refusing unsafe --base-url: {exc}", file=sys.stderr)
            return 2

    session: Any = public_session()
    if args.auth_env:
        token = os.environ.get(args.auth_env, "")
        if not token:
            print(f"{args.auth_env} is unset.", file=sys.stderr)
            return 1
        from .access import static_session

        session = static_session({"Authorization": f"Bearer {token}"})

    if args.registry:
        if args.emit_dir:
            print("--emit-dir is not supported with --registry.", file=sys.stderr)
            return 2

        from .registry.client import RegistryFetchError, fetch_surface

        try:
            fetched = fetch_surface(
                args.registry_url,
                args.registry,
                key=os.environ.get("GECKO_API_KEY") or None,
            )
        except RegistryFetchError as exc:
            print(f"Could not fetch surface: {exc}", file=sys.stderr)
            return 2
        if fetched.stale:
            # Informational, not an error — the surface IS being served, just from
            # the cache. In stdio mode stdout is the JSON-RPC channel, so this human
            # note goes to stderr; otherwise it rides with the banner on stdout.
            print(
                "registry unreachable — serving the last cached copy (stale).",
                file=sys.stderr if args.stdio else sys.stdout,
            )
        if args.auth_keychain:
            # The spec is already in hand (no extra fetch) — derive header/scheme
            # from its own declared security scheme, never a hardcoded Bearer.
            session, warning = keychain_session(fetched.spec, args.auth_keychain)
            if warning:
                print(warning, file=sys.stderr)
        try:
            client = AgentApiClient(
                fetched.spec, session=session, base_url=args.base_url
            )
        except CredentialError as exc:
            print(f"auth: {exc}", file=sys.stderr)
            return 1
        # No --name given: the registry key IS the surface name (deliberate).
        name = args.name or args.registry
    else:
        # Early, friendly SSRF check for URL specs (ingest re-validates while
        # fetching).
        if args.spec.startswith(("http://", "https://")):
            try:
                validate_public_url(args.spec)
            except UnsafeUrlError as exc:
                print(f"Refusing to ingest unsafe URL: {exc}", file=sys.stderr)
                return 2

        try:
            if args.auth_keychain:
                # A second, SSRF-safe load solely to read the security scheme —
                # the ORIGINAL spec string/path still goes to AgentApiClient
                # below unchanged, so the trust-anchor pinning (surfaces.anchor_for)
                # is untouched.
                from .ingest import load_spec

                session, warning = keychain_session(
                    load_spec(args.spec), args.auth_keychain
                )
                if warning:
                    print(warning, file=sys.stderr)
            client = AgentApiClient(args.spec, session=session, base_url=args.base_url)
        except (UnsafeUrlError, ValueError) as exc:
            print(f"Could not comprehend spec: {exc}", file=sys.stderr)
            return 2
        except CredentialError as exc:
            print(f"auth: {exc}", file=sys.stderr)
            return 1

        title = str((client.spec.get("info") or {}).get("title", ""))
        name = args.name or _slugify(title)

    mcp_url = _mcp_url(args.host, args.port, args.public_url)

    extra_hosts: list[str] = list(args.allow_host)
    extra_origins: list[str] = list(args.allow_origin)
    if args.public_url:
        # Trust the advertised public URL's host/origin (tunnel/DNS-rebinding guard).
        from urllib.parse import urlsplit

        parts = urlsplit(args.public_url)
        if parts.netloc:
            extra_hosts.append(parts.netloc)
            extra_origins.append(f"{parts.scheme}://{parts.netloc}")

    # Provider hand-off: emit the discovery files and exit (no server).
    if args.emit_dir:
        emit_mcp = mcp_url if args.public_url else None
        return _emit(client, args.emit_dir, emit_mcp, args.site_url)

    # stdio: the client spawns this process and talks over stdin/stdout. NOTHING may
    # go to stdout except the JSON-RPC stream — the startup note goes to stderr, and
    # no HTTP port is bound. Same comprehended surface + call-time auth injection.
    if args.stdio:
        print(
            f"Serving '{name}' over stdio ({_summary(client)}). "
            "Control plane only — no data, no payloads, no secrets stored.",
            file=sys.stderr,
        )
        serve_stdio(client, mode=args.mode, server_name=name)
        return 0

    _print_banner(name, mcp_url, _summary(client), _stdio_spawn(args))

    serve_http(
        client,
        host=args.host,
        port=args.port,
        mode=args.mode,
        server_name=name,
        allowed_hosts=extra_hosts or None,
        allowed_origins=extra_origins or None,
        public_url=args.public_url,
    )
    return 0


def _run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _run()
