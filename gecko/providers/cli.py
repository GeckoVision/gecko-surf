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

from ..provider_config import load_packaged_provider, load_packaged_provider_base_url
from .orquestra import Intent, OrquestraProgramSurface

__all__ = ["main", "PROGRAMS", "build_surface_from_config"]


def build_surface_from_config(
    provider: str, api_id: str, intents: dict[str, Intent]
) -> OrquestraProgramSurface:
    """Build a program surface from packaged config (identity + PDA recipes) + a
    supplied intent registry (the plan callables). This is the config-driven
    backbone: PDAs are DATA, only the multi-step plan is code."""
    _, apis = load_packaged_provider(provider)
    api = apis[api_id]
    if api.program is None:
        raise ValueError(f"api {api_id!r} of provider {provider!r} is not a program")
    if api.program.orquestra_project is None:
        raise ValueError(
            f"api {api_id!r} has no orquestra_project — comprehended for derivation only, "
            "not yet servable (no execute/build URL wired)"
        )
    base = load_packaged_provider_base_url(provider).rstrip("/")
    project_base_url = f"{base}/{api.program.orquestra_project}"
    wanted = {k: v for k, v in intents.items() if k in set(api.program.intents)}
    return OrquestraProgramSurface(
        program_id=api.program.program_id,
        project_base_url=project_base_url,
        pdas=dict(api.program.pdas),
        intents=wanted,
    )


def _discover_programs() -> dict[str, Callable[[], OrquestraProgramSurface]]:
    """Discover servable programs from packaged config. Each config-listed program
    is paired with its code-side intent registry (the plan callables)."""
    from .meteora import METEORA_INTENTS
    from .pumpfun import PUMPFUN_INTENTS

    # (provider, api_id) → the intent registry supplying that program's plan callables.
    # ore/metadao_ico are servable for derive_pda + get_program_graph but carry no plan
    # intents yet (execute plans land in a later sprint) → empty registries.
    intents_by_key: dict[tuple[str, str], dict[str, Intent]] = {
        ("orquestra", "meteora"): METEORA_INTENTS,
        ("orquestra", "pumpfun"): PUMPFUN_INTENTS,
        ("orquestra", "ore"): {},
        ("orquestra", "metadao_ico"): {},
    }
    programs: dict[str, Callable[[], OrquestraProgramSurface]] = {}
    for (provider, api_id), intents in intents_by_key.items():

        def _make(
            provider: str = provider,
            api_id: str = api_id,
            intents: dict[str, Intent] = intents,
        ) -> OrquestraProgramSurface:
            return build_surface_from_config(provider, api_id, intents)

        programs[api_id] = _make
    return programs


# The provider's program registry — program name → surface builder, discovered from
# packaged config. Add a program by writing its config + registering its intents in
# _discover_programs; no new CLI entry.
PROGRAMS: dict[str, Callable[[], OrquestraProgramSurface]] = _discover_programs()


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
