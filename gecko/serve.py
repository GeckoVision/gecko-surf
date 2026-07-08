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

from .access import public_session
from .client import AgentApiClient
from .deeplinks import all_add_strings
from .http_server import MCP_PATH, serve_http
from .netguard import UnsafeUrlError, validate_public_url


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
        default="https://mcp.geckovision.tech",
        help="Registry base URL.",
    )
    p.add_argument(
        "--auth-env",
        default=None,
        help="Env var holding the PROVIDER bearer token — injected locally at "
        "call time, never sent to Gecko.",
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


def _print_banner(name: str, mcp_url: str, summary: str) -> None:
    print("Gecko — make any API agent-usable (gecko-surf)\n" + "=" * 56)
    print(summary)
    print("Control plane: Gecko stores only the API surface — never your data,")
    print("never response payloads, never secrets.\n")
    print(f"MCP URL (Streamable HTTP):  {mcp_url}\n")
    print("Add it to an agent (one step):")
    adds = all_add_strings(name, mcp_url)
    print(f"  Claude Code:  {adds['claude']}")
    print(f"  Cursor:       {adds['cursor']}")
    print(f"  VS Code:      {adds['vscode']}\n")
    # Self-diagnose the #1 activation failure: the client reports "connected" but loads
    # ZERO tools. That means its MCP transport can't reach 127.0.0.1 — a sandboxed /
    # remote agent runs its MCP client in a different network namespace than this shell.
    # Give the fix at the moment it bites, so a first-time user doesn't silently bounce.
    if mcp_url.startswith("http://127.0.0.1") or mcp_url.startswith("http://localhost"):
        print("Connected but your agent shows 0 tools? Its MCP client can't reach")
        print(
            "localhost (sandboxed/remote agents run in a separate network namespace)."
        )
        print("Serve behind a public URL instead:")
        print("  cloudflared tunnel --url " + mcp_url.rsplit("/mcp", 1)[0])
        print("  gecko <spec> --public-url https://<name>.trycloudflare.com\n")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if bool(args.spec) == bool(args.registry):
        print("Provide exactly one of <spec> or --registry <name>.", file=sys.stderr)
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
            # the cache — so stdout (like the banner), not stderr.
            print("registry unreachable — serving the last cached copy (stale).")
        client = AgentApiClient(fetched.spec, session=session)
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
            client = AgentApiClient(args.spec, session=session)
        except (UnsafeUrlError, ValueError) as exc:
            print(f"Could not comprehend spec: {exc}", file=sys.stderr)
            return 2

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

    _print_banner(name, mcp_url, _summary(client))

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
