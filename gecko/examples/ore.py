"""Serve the ORE program's instruction↔PDA graph — the seeds an IDL/llms.txt loses.

ORE (``oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv``) is a Steel-framework Solana
program: it emits NO Anchor IDL, so its PDA seed recipes live only in source. Gecko
comprehends the bundled source slice (``ore_program_pdas.rs``) into first-call-correct
``derive_pda`` / ``plan_instruction`` / ``get_program_graph`` tools an agent calls
directly — keyless, no signing, control-plane only.

    # zero-friction over stdio:
    claude mcp add ore -- uvx --from "gecko-surf[serve]" ore-mcp --stdio
    # or serve over HTTP:
    uvx --from "gecko-surf[serve]" ore-mcp

Also mounted on the hosted multi-surface server (``build_ore_surface`` in
``serve_mcp``), so an agent can add ``<host>/ore/mcp`` and derive ORE's PDAs live.
"""

from __future__ import annotations

import argparse
import sys
from importlib import resources

from gecko.program_mcp import ProgramGraphSurface, build_program_surface

__all__ = ["ORE_PROGRAM_ID", "build_ore_surface", "main"]

# ORE program id (mainnet) — api/src/lib.rs `declare_id!`.
ORE_PROGRAM_ID = "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"


def _bundled_source() -> str:
    """Read the packaged ORE PDA source slice (ships in the wheel under gecko.examples)."""
    return (
        resources.files("gecko.examples")
        .joinpath("ore_program_pdas.rs")
        .read_text(encoding="utf-8")
    )


def build_ore_surface() -> ProgramGraphSurface:
    """Comprehend the bundled ORE source into a servable PDA-graph MCP surface."""
    return build_program_surface(source=_bundled_source(), program_id=ORE_PROGRAM_ID)


def main(argv: list[str] | None = None) -> int:
    """CLI entry (``ore-mcp``): serve the bundled ORE PDA graph over MCP. Needs [serve]."""
    parser = argparse.ArgumentParser(
        prog="ore-mcp",
        description="Serve the ORE program's PDA graph over MCP (keyless).",
    )
    parser.add_argument(
        "--stdio", action="store_true", help="serve over stdio instead of HTTP"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--public-url", default=None, help="external URL when behind a tunnel/ALB"
    )
    args = parser.parse_args(argv)

    surface = build_ore_surface()
    print(
        f"ore-mcp: {len(surface.graph.pdas)} PDA recipe(s) for {ORE_PROGRAM_ID}",
        file=sys.stderr,  # stdout is the stdio JSON-RPC channel
    )

    if args.stdio:
        from gecko.mcp_server import serve_stdio

        serve_stdio(surface, server_name="ore")
    else:
        from gecko.http_server import serve_http

        serve_http(
            surface,
            host=args.host,
            port=args.port,
            mode="recorded",
            server_name="ore",
            public_url=args.public_url,
        )
    return 0
