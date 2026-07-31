"""``gecko-orquestra`` — serve an Orquestra program's front-door surface over MCP.

The entry is the **provider** (Orquestra); the **program** is a parameter — one command
for all of Orquestra's programs, parameterized, never a new entry per program (the
per-provider model: docs/specs/2026-07-31-orquestra-provider-integration.md).

    gecko-orquestra --program meteora --stdio      # add straight into Claude Code
    gecko-orquestra --program meteora              # or serve over HTTP

Adding a program later (pump, jupiter, …) is a new entry in ``_PROGRAMS`` + its recipes —
not a new CLI. Keyless, control plane only: it derives the accounts and points at
Orquestra's builder; it never proxies or signs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

from .orquestra import OrquestraProgramSurface

__all__ = ["main", "PROGRAMS"]

# The provider's program registry — program name → surface builder. Add an instance here
# (config + recipes) to expose a new Orquestra program; no new CLI entry.
from .meteora import build_meteora_surface

PROGRAMS: dict[str, Callable[[], OrquestraProgramSurface]] = {
    "meteora": build_meteora_surface,
}


def serve(surface: OrquestraProgramSurface, args: argparse.Namespace, name: str) -> int:
    """Serve a built provider surface over stdio or Streamable-HTTP."""
    print(
        f"gecko-orquestra[{name}]: {len(surface.pdas)} PDA(s), intents={list(surface.intents)} "
        f"→ executes via {surface.project_base_url}",
        file=sys.stderr,  # stdout is the stdio JSON-RPC channel
    )
    if args.stdio:
        from ..mcp_server import serve_stdio

        serve_stdio(surface, server_name=name)
    else:
        from ..http_server import serve_http

        serve_http(
            surface,
            host=args.host,
            port=args.port,
            mode="recorded",
            server_name=name,
            public_url=args.public_url,
        )
    return 0


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stdio", action="store_true", help="serve over stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--public-url", default=None, help="external URL behind a tunnel/ALB"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry (``gecko-orquestra``): serve ``--program <name>`` over MCP. Needs [serve]."""
    parser = argparse.ArgumentParser(
        prog="gecko-orquestra",
        description="Serve an Orquestra program's front-door surface over MCP (keyless).",
    )
    parser.add_argument(
        "--program",
        required=True,
        choices=sorted(PROGRAMS),
        help="which Orquestra program to serve",
    )
    _add_serve_args(parser)
    args = parser.parse_args(argv)

    surface = PROGRAMS[args.program]()
    return serve(surface, args, name=args.program)
