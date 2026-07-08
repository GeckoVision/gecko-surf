"""Serve the Colosseum Copilot API to your agent — first-call-correct, BYOK.

The surface is fetched from the Gecko registry first (the freshest comprehended
snapshot); on any registry failure (offline, older registry, network hiccup) it
falls back silently to the surface bundled *inside* the package (comprehended from
Colosseum's docs — no OpenAPI is published), so there is never a hard dependency
on network access:

    export COLOSSEUM_COPILOT_PAT=...      # https://arena.colosseum.org/copilot
    uvx --from "gecko-surf[serve]" colosseum-mcp
    claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp

Your PAT is injected at call time, hidden from the agent, and sent only to Colosseum's
pinned host — Gecko refuses to leak a secret to any other host.

NOTE: the default bind is loopback, which assumes the MCP client and this server share
a network namespace. Sandboxed agent harnesses often don't (their MCP client runs in a
different network context than their shell, so ``claude mcp list`` says Connected while
the session loads zero tools). For those, serve behind a real URL:

    cloudflared tunnel --url http://127.0.0.1:8000
    colosseum-mcp --public-url https://<name>.trycloudflare.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from gecko.client import AgentApiClient

BASE = "https://copilot.colosseum.com/api/v1"


@dataclass
class BearerSession:
    """Injects the PAT as a bearer token. (Gecko's caller supplies a real User-Agent by
    default, so Colosseum's Cloudflare WAF doesn't 403 the stdlib client.)"""

    token: str

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def load_spec() -> dict[str, Any]:
    """Load the packaged OpenAPI stub from importable package data (works from the
    installed wheel, not a cwd-relative path)."""
    text = (
        resources.files("gecko.examples")
        .joinpath("colosseum_copilot_openapi.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)  # type: ignore[no-any-return]


def build_client(pat: str) -> AgentApiClient:
    # base_url pins the trust anchor to Colosseum's host, so Gecko will inject the PAT
    # (it degrades to a $0 recorded call rather than fire a secret at an unpinned host).
    return AgentApiClient(load_spec(), base_url=BASE, session=BearerSession(pat))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The same four networking flags as ``gecko serve`` — the console entry must not
    be *less* reachable than the generic CLI (loopback-only broke sandboxed harnesses
    whose MCP client doesn't share the shell's network namespace)."""
    p = argparse.ArgumentParser(
        prog="colosseum-mcp",
        description="Serve the Colosseum Copilot API to your agent over MCP (BYOK).",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    p.add_argument("--port", type=int, default=8000, help="Bind port (default 8000).")
    p.add_argument(
        "--public-url",
        default=None,
        help="Public HTTPS URL the agent will connect to (e.g. a tunnel). "
        "Advertised in the add string and trusted for Host/Origin.",
    )
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Extra Host header to allow (repeatable; for a tunnel hostname).",
    )
    return p.parse_args(argv)


def _mcp_url(host: str, port: int, public_url: str | None) -> str:
    if public_url:
        base = public_url.rstrip("/")
        return base if base.endswith("/mcp") else base + "/mcp"
    return f"http://{host}:{port}/mcp"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    pat = os.environ.get("COLOSSEUM_COPILOT_PAT")
    if not pat:
        print(
            "Set COLOSSEUM_COPILOT_PAT — get one at https://arena.colosseum.org/copilot",
            file=sys.stderr,
        )
        return 1
    from gecko.http_server import serve_http  # optional [serve] deps, imported lazily

    # Trust the advertised public URL's host/origin (tunnel/DNS-rebinding guard) —
    # same move as gecko.serve, so a tunnel works without hand-listing its hostname.
    extra_hosts: list[str] = list(args.allow_host)
    extra_origins: list[str] = []
    if args.public_url:
        parts = urlsplit(args.public_url)
        if parts.netloc:
            extra_hosts.append(parts.netloc)
            extra_origins.append(f"{parts.scheme}://{parts.netloc}")

    spec: dict[str, Any]
    source = "bundled"
    try:
        from gecko.registry.client import fetch_surface

        fetched = fetch_surface(
            os.environ.get("GECKO_REGISTRY_URL", "https://mcp.geckovision.tech"),
            "colosseum",
        )
        spec, source = fetched.spec, f"registry rev {fetched.surface_rev[:8]}"
    except Exception:  # noqa: BLE001 - offline/older registry: bundled still works
        spec = load_spec()
    client = AgentApiClient(spec, base_url=BASE, session=BearerSession(pat))
    mcp_url = _mcp_url(args.host, args.port, args.public_url)
    print(
        f"Colosseum Copilot — {len(client.list_tools())} first-call-correct tools ready."
    )
    print(f"surface source: {source}")
    print("PAT injected at call time, hidden from the agent, sent only to Colosseum.")
    print(f"Add it:  claude mcp add --transport http colosseum {mcp_url}")
    serve_http(
        client,
        host=args.host,
        port=args.port,
        mode="live",
        allowed_hosts=extra_hosts or None,
        allowed_origins=extra_origins or None,
        public_url=args.public_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
