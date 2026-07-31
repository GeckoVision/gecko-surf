"""Meteora DLMM — the first Orquestra provider instance (the touchable Berkay demo).

Wires the generic Orquestra surface (:mod:`gecko.providers.orquestra`) to Meteora DLMM:
its program id, the recovered PDA recipes, the Orquestra project's build base, and a
``swap`` intent. The star is ``lb_pair`` — the helper-seeded root Orquestra returns
``"has no PDA seeds"`` for; here it's derivable, and the whole account set falls out.

The PDA recipes below are the program's **public on-chain seed facts** for Meteora DLMM
(`LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo`) — authored as data (not copied source), so
this ships license-clean. `lb_pair = [min(x,y), max(x,y), bin_step]`, `reserve = [lb_pair,
token_mint]`, `oracle = [b"oracle", lb_pair]`.

Run it (published 0.9.1+):
    # stdio, add straight into Claude Code:
    claude mcp add meteora -- uvx --from "gecko-surf[serve,solana]" meteora-demo --stdio
    # or HTTP:
    uvx --from "gecko-surf[serve,solana]" meteora-demo
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping

from ..pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    VariablePdaSeedNode,
)
from .orquestra import Intent, OrquestraProgramSurface

__all__ = ["METEORA_PROGRAM_ID", "build_meteora_surface", "main"]

METEORA_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
# Orquestra's indexed Meteora DLMM project (public, keyless consumption).
ORQUESTRA_PROJECT_BASE = "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl"


def _pdas() -> dict[str, PdaNode]:
    return {
        # the root Orquestra drops: min/max pool-pair ordering + bin_step
        "lb_pair": PdaNode(
            "lb_pair",
            (
                OrderedPairPdaSeedNode("token_x_mint", "token_y_mint", "min"),
                OrderedPairPdaSeedNode("token_x_mint", "token_y_mint", "max"),
                VariablePdaSeedNode(
                    "bin_step", source="argument", encoding="le", width=2
                ),
            ),
            program_id=METEORA_PROGRAM_ID,
        ),
        # leaves — seed FROM lb_pair (Orquestra has these, once you have the root)
        "reserve": PdaNode(
            "reserve",
            (
                VariablePdaSeedNode("lb_pair", source="account", encoding="pubkey"),
                VariablePdaSeedNode("token_mint", source="account", encoding="pubkey"),
            ),
            program_id=METEORA_PROGRAM_ID,
        ),
        "oracle": PdaNode(
            "oracle",
            (
                ConstantPdaSeedNode(b"oracle", encoding="utf8"),
                VariablePdaSeedNode("lb_pair", source="account", encoding="pubkey"),
            ),
            program_id=METEORA_PROGRAM_ID,
        ),
    }


def _swap_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Derive the full account set for a swap — lb_pair first (the root), then the leaves."""
    x, y, bin_step = (
        str(args["input_mint"]),
        str(args["output_mint"]),
        int(args["bin_step"]),
    )
    lb_pair = surface.derive(
        "lb_pair", {"token_x_mint": x, "token_y_mint": y, "bin_step": bin_step}
    )
    return {
        "lb_pair": lb_pair,  # ← the address Orquestra can't derive
        "reserve_x": surface.derive("reserve", {"lb_pair": lb_pair, "token_mint": x}),
        "reserve_y": surface.derive("reserve", {"lb_pair": lb_pair, "token_mint": y}),
        "oracle": surface.derive("oracle", {"lb_pair": lb_pair}),
    }


_SWAP = Intent(
    name="plan_swap",
    instruction="swap",
    description=(
        "Plan a Meteora DLMM swap from two token mints. Give input_mint, output_mint and "
        "bin_step; Gecko derives the pool (lb_pair) and its reserves/oracle — the accounts "
        "Orquestra's IDL can't derive — and returns the plan pointing at Orquestra's swap "
        "builder to execute."
    ),
    inputs=("input_mint", "output_mint", "bin_step"),
    plan=_swap_plan,
)


def build_meteora_surface() -> OrquestraProgramSurface:
    return OrquestraProgramSurface(
        program_id=METEORA_PROGRAM_ID,
        project_base_url=ORQUESTRA_PROJECT_BASE,
        pdas=_pdas(),
        intents={_SWAP.name: _SWAP},
    )


def main(argv: list[str] | None = None) -> int:
    """CLI (``meteora-demo``): serve the Meteora provider surface over MCP. Needs [serve]."""
    parser = argparse.ArgumentParser(
        prog="meteora-demo",
        description="Gecko × Orquestra: derive Meteora's lb_pair + plan a swap (keyless).",
    )
    parser.add_argument("--stdio", action="store_true", help="serve over stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--public-url", default=None)
    args = parser.parse_args(argv)

    surface = build_meteora_surface()
    print(
        f"meteora-demo: {len(surface.pdas)} PDA(s), intents={list(surface.intents)} "
        f"→ executes via {surface.project_base_url}",
        file=sys.stderr,
    )
    if args.stdio:
        from ..mcp_server import serve_stdio

        serve_stdio(surface, server_name="meteora")
    else:
        from ..http_server import serve_http

        serve_http(
            surface,
            host=args.host,
            port=args.port,
            mode="recorded",
            server_name="meteora",
            public_url=args.public_url,
        )
    return 0
