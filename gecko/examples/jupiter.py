"""Serve the Jupiter Swap API to your agent — first-call-correct, keyless by default.

The surface is fetched from the Gecko registry first (the freshest comprehended
snapshot); on any registry failure (offline, older registry, network hiccup) it
falls back silently to the surface bundled *inside* the package (comprehended from
Jupiter's published ``swagger.yaml``), so there is never a hard dependency on network
access:

    uvx --from "gecko-surf[serve]" jupiter-mcp
    claude mcp add --transport http jupiter http://127.0.0.1:8000/mcp

Jupiter's Swap API is public and keyless — this serves the free tier against
``lite-api.jup.ag`` with no credential at all. To use a Pro key (higher rate
limits), set ``JUPITER_API_KEY`` and the server switches to the ``api.jup.ag`` host,
injecting the key as ``x-api-key`` at call time — hidden from the agent and sent only
to Jupiter's pinned host (Gecko refuses to leak a secret to any other host).

    export JUPITER_API_KEY=...            # optional, from portal.jup.ag
    uvx --from "gecko-surf[serve]" jupiter-mcp

NOTE: the default bind is loopback, which assumes the MCP client and this server share
a network namespace. Sandboxed agent harnesses often don't (their MCP client runs in a
different network context than their shell, so ``claude mcp list`` says Connected while
the session loads zero tools). For those, serve behind a real URL:

    cloudflared tunnel --url http://127.0.0.1:8000
    jupiter-mcp --public-url https://<name>.trycloudflare.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from gecko.client import AgentApiClient

# Jupiter splits hosts by tier (verified against dev.jup.ag, 2026-07-10): the free
# tier is keyless on lite-api.jup.ag; api.jup.ag is the API-key host. The bundled
# spec's server is the paid host, so keyless serving must retarget to lite-api.
BASE_KEYLESS = "https://lite-api.jup.ag/swap/v1"
BASE_PRO = "https://api.jup.ag/swap/v1"

# Optional Pro credential. Read AT CALL TIME (never stored), injected only when set.
API_KEY_ENV = "JUPITER_API_KEY"
API_KEY_HEADER = "x-api-key"


def load_spec() -> dict[str, Any]:
    """Load the packaged OpenAPI spec from importable package data (works from the
    installed wheel, not a cwd-relative path)."""
    text = (
        resources.files("gecko.examples")
        .joinpath("jupiter_swap_openapi.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(text)  # type: ignore[no-any-return]


def _pro_session() -> Any:
    """A live session that resolves ``JUPITER_API_KEY`` from the environment AT CALL
    TIME and injects it as ``x-api-key`` — never storing the value on the instance.
    A dedicated env-only resolver keeps the mapping local to this example (the engine's
    global credential table stays untouched)."""
    from gecko.access import ResolvedSession
    from gecko.credentials import ChainResolver, CredentialRef, EnvBackend

    resolver = ChainResolver([EnvBackend(legacy_names={"jupiter": API_KEY_ENV})])
    return ResolvedSession(
        ref=CredentialRef(api="jupiter"),
        header_name=API_KEY_HEADER,
        scheme="raw",
        resolver=resolver,
    )


def build_client(
    spec: dict[str, Any] | None = None, *, pro: bool = False
) -> AgentApiClient:
    """Build the client for the chosen tier. ``base_url`` pins the trust anchor to
    Jupiter's host, so a Pro key is injected only there (and degrades to a $0 recorded
    call rather than fire a secret at an unpinned host). Keyless serves a no-auth
    session against lite-api — all four ops are ungated, so all stay visible."""
    from gecko.access import public_session

    spec = spec if spec is not None else load_spec()
    if pro:
        return AgentApiClient(spec, base_url=BASE_PRO, session=_pro_session())
    return AgentApiClient(spec, base_url=BASE_KEYLESS, session=public_session())


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The same four networking flags as ``gecko serve`` — the console entry must not
    be *less* reachable than the generic CLI (loopback-only broke sandboxed harnesses
    whose MCP client doesn't share the shell's network namespace)."""
    p = argparse.ArgumentParser(
        prog="jupiter-mcp",
        description="Serve the Jupiter Swap API to your agent over MCP (keyless by default).",
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


def _verify_key(client: Any, has_key: bool) -> tuple[bool, str]:
    """When a Pro key is set, probe the label endpoint so an INVALID key fails loudly
    HERE — at startup, before the agent connects — instead of as an opaque tool-call
    error after a successful MCP handshake. Returns ``(ok_to_serve, message)``. Keyless
    serving always proceeds. Fail-open on any transient/network error: a blip must never
    block serving, only a real 401/403 aborts. The key itself is never printed."""
    if not has_key:
        return (
            True,
            "Serving keyless (free tier via lite-api.jup.ag) — no JUPITER_API_KEY set.",
        )
    tool = next(
        (
            t["name"]
            for t in client.list_tools()
            if "programidtolabel" in t["name"].lower()
        ),
        None,
    )
    if tool is None:
        return True, "(no lightweight endpoint to verify the key against; continuing)"
    try:
        code = client.call(tool, {}, mode="live").get("status")
    except Exception:  # noqa: BLE001 - a transient error must not block serving
        return True, "(could not reach Jupiter to verify the key; continuing)"
    if code in (401, 403):
        return False, (
            "JUPITER_API_KEY is invalid — check your Pro key at https://portal.jup.ag"
        )
    if isinstance(code, int) and 200 <= code < 300:
        return True, "JUPITER_API_KEY verified — Jupiter Pro authenticated."
    return True, f"(key check returned HTTP {code}; continuing)"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    has_key = bool(os.environ.get(API_KEY_ENV))

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
            "jupiter",
        )
        spec, source = fetched.spec, f"registry rev {fetched.surface_rev[:8]}"
    except Exception:  # noqa: BLE001 - offline/older registry: bundled still works
        spec = load_spec()
    client = build_client(spec, pro=has_key)

    # Fail loudly on a bad Pro key before the agent ever connects (keyless just proceeds).
    key_ok, key_msg = _verify_key(client, has_key)
    print(key_msg, file=sys.stdout if key_ok else sys.stderr)
    if not key_ok:
        return 1

    mcp_url = _mcp_url(args.host, args.port, args.public_url)
    print(f"Jupiter Swap — {len(client.list_tools())} first-call-correct tools ready.")
    print(f"surface source: {source}")
    if has_key:
        print(
            "JUPITER_API_KEY injected at call time, hidden from the agent, sent only to Jupiter."
        )
    print(f"Add it:  claude mcp add --transport http jupiter {mcp_url}")
    # Self-diagnose the "connected but 0 tools" failure a sandboxed/remote agent hits
    # when its MCP client can't reach loopback (separate network namespace).
    if mcp_url.startswith(("http://127.0.0.1", "http://localhost")):
        print(
            "0 tools in your agent after it connects? Its MCP client can't reach "
            "localhost.\n  Serve behind a tunnel:  cloudflared tunnel --url "
            f"http://{args.host}:{args.port}\n"
            "  then re-run with:  jupiter-mcp --public-url https://<name>.trycloudflare.com"
        )
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
