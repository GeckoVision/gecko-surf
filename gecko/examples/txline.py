"""Serve the TxLINE off-chain API (TxODDS) to your agent — first-call-correct,
two-token auth injected, nothing to download.

TxLINE publishes no public OpenAPI URL and gates its data behind TWO headers at
once — ``Authorization: Bearer <jwt>`` AND ``X-Api-Token: <tok>``. This bundled
surface removes all that friction: the comprehended spec ships *inside* the
package, so there is no local file and no URL to find, and both tokens are
injected at call time (hidden from the agent, sent only to TxLINE's pinned host).

    # zero-friction (recommended): your agent spawns it over stdio — no port, no tunnel
    claude mcp add txline -- uvx --from "gecko-surf[serve]" txline-mcp --mode live --stdio
    # or over HTTP for a remote/shared client:
    uvx --from "gecko-surf[serve]" txline-mcp --mode live
    claude mcp add --transport http txline http://127.0.0.1:8000/mcp

Seal your two tokens once (hidden prompt, OS keychain, never a file) — then live
calls just work:

    gecko auth set txline --account httpAuth --scheme bearer   # the Bearer JWT
    gecko auth set txline --account apiKeyAuth                  # the X-Api-Token

``--mode recorded`` (the default) serves synthesized $0 responses with NO tokens
at all — kick the tires offline first, then flip to ``--mode live``.

NOTE: the default bind is loopback, which assumes the MCP client and this server
share a network namespace. Sandboxed agent harnesses often don't — prefer
``--stdio`` (no port, no tunnel), or serve behind a tunnel with ``--public-url``.
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import yaml

from gecko.client import AgentApiClient
from gecko.credentials import CredentialError
from gecko.deeplinks import claude_stdio_add_command

# The exact command a client spawns to run this surface over stdio (no port, no tunnel).
STDIO_SPAWN = 'uvx --from "gecko-surf[serve]" txline-mcp --mode live --stdio'

# TxLINE's production host — the trust anchor a live call's tokens are pinned to.
BASE_URL = "https://txline.txodds.com"

# The keychain surface name the two per-scheme credentials are sealed under.
SURFACE = "txline"

# Public copy of the same comprehended spec, for the frozen binary (which does not
# bundle package data): loaded only if the in-package spec can't be read.
SPEC_URL = (
    "https://raw.githubusercontent.com/GeckoVision/gecko-surf/"
    "main/examples/txline_demo/spec/txline_openapi.yaml"
)


def load_spec() -> dict[str, Any]:
    """The comprehended TxLINE spec. Prefer the copy bundled *inside* the package
    (offline, no network); if it isn't present — a PyInstaller onefile binary does
    not carry package data — fall back to the public raw copy over the network
    (TxLINE is a network API anyway, so this never adds a NEW dependency)."""
    try:
        text = (
            resources.files("gecko.examples")
            .joinpath("txline_openapi.yaml")
            .read_text(encoding="utf-8")
        )
        spec = yaml.safe_load(text)
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        from gecko.ingest import load_spec as load_remote

        return load_remote(SPEC_URL)
    if not isinstance(spec, dict):
        raise ValueError("bundled TxLINE spec did not parse to a mapping")
    return spec


def build_client(
    spec: dict[str, Any] | None = None, *, mode: str = "recorded"
) -> AgentApiClient:
    """Build the client. ``base_url`` pins the trust anchor to TxLINE's host so the
    two tokens are injected only there. In ``recorded`` mode a stub session keeps
    every auth-gated tool VISIBLE while calls are synthesized $0 (no real tokens);
    in ``live`` mode the session resolves both tokens from the keychain at call
    time via the spec's own two security schemes."""
    from gecko.access import keychain_session, stub_session

    spec = spec if spec is not None else load_spec()
    if mode == "live":
        session, warning = keychain_session(spec, SURFACE)
        if warning:
            print(warning, file=sys.stderr)
    else:
        session = stub_session()  # both headers present -> gated tools stay visible
    return AgentApiClient(spec, base_url=BASE_URL, session=session)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The same networking flags as ``gecko serve`` — the console entry must not be
    *less* reachable than the generic CLI — plus ``--mode`` (recorded/$0 vs live)."""
    p = argparse.ArgumentParser(
        prog="txline-mcp",
        description="Serve the TxLINE off-chain API (TxODDS) to your agent over MCP.",
    )
    p.add_argument(
        "--mode",
        choices=("recorded", "live"),
        default="recorded",
        help="recorded ($0, synthesized, no tokens) or live (real TxLINE calls, "
        "two tokens from the keychain).",
    )
    p.add_argument(
        "--stdio",
        action="store_true",
        help="Serve over stdio (the client SPAWNS this process; no port, no tunnel) "
        "instead of HTTP — the zero-friction local path.",
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

    spec = load_spec()
    try:
        client = build_client(spec, mode=args.mode)
    except CredentialError as exc:
        # Live mode before both tokens are sealed: guide precisely, never crash with a
        # traceback (constructing the client resolves the session to decide tool
        # visibility, which is where an unsealed keychain surfaces).
        from gecko.access import auth_setup_hint

        print(f"auth: {exc}", file=sys.stderr)
        hint = auth_setup_hint(spec, SURFACE)
        if hint:
            print(hint, file=sys.stderr)
        print(
            "Seal both tokens (above), then re-run — or try --mode recorded ($0, no "
            "tokens) first.",
            file=sys.stderr,
        )
        return 1
    n = len(client.list_tools())

    # Trust the advertised public URL's host/origin (tunnel/DNS-rebinding guard).
    extra_hosts: list[str] = list(args.allow_host)
    extra_origins: list[str] = []
    if args.public_url:
        parts = urlsplit(args.public_url)
        if parts.netloc:
            extra_hosts.append(parts.netloc)
            extra_origins.append(f"{parts.scheme}://{parts.netloc}")

    if args.stdio:
        from gecko.mcp_server import serve_stdio  # optional [serve] deps, lazy

        print(
            f"TxLINE — {n} first-call-correct tools ready (stdio, {args.mode} mode).",
            file=sys.stderr,
        )
        serve_stdio(client, mode=args.mode, server_name="txline")
        return 0

    from gecko.http_server import serve_http  # optional [serve] deps, lazy

    mcp_url = _mcp_url(args.host, args.port, args.public_url)
    print(f"TxLINE — {n} first-call-correct tools ready ({args.mode} mode).\n")
    print(
        "Recommended — zero-friction stdio (no port, no tunnel; your agent spawns it):"
    )
    print(f"  {claude_stdio_add_command('txline', STDIO_SPAWN)}\n")
    print("Serving to a remote or shared client? Use HTTP instead:")
    print(f"  claude mcp add --transport http txline {mcp_url}")
    if mcp_url.startswith(("http://127.0.0.1", "http://localhost")):
        print("Connected but your agent shows 0 tools?")
        print("  Prefer --stdio (above) — it removes the localhost problem entirely.")
    serve_http(
        client,
        host=args.host,
        port=args.port,
        mode=args.mode,
        allowed_hosts=extra_hosts or None,
        allowed_origins=extra_origins or None,
        public_url=args.public_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
