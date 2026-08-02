"""Meteora DLMM — the first Orquestra provider instance (the touchable Berkay demo).

Wires the generic Orquestra surface (:mod:`gecko.providers.orquestra`) to Meteora DLMM:
its program id, the recovered PDA recipes, the Orquestra project's build base, and a
``swap`` intent. The star is ``lb_pair`` — the helper-seeded root Orquestra returns
``"has no PDA seeds"`` for; here it's derivable, and the whole account set falls out.

The PDA recipes below are the program's **public on-chain seed facts** for Meteora DLMM
(`LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo`) — authored as data (not copied source), so
this ships license-clean. `lb_pair = [min(x,y), max(x,y), bin_step]`, `reserve = [lb_pair,
token_mint]`, `oracle = [b"oracle", lb_pair]`.

Run it (published 0.9.3+):
    # stdio, add straight into Claude Code:
    claude mcp add orquestra-meteora -- \
        uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program meteora --stdio
    # or HTTP:
    uvx --from "gecko-surf[serve,solana]" gecko-orquestra --program meteora
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from .orquestra import Intent, OrquestraProgramSurface

__all__ = ["METEORA_PROGRAM_ID", "METEORA_INTENTS", "build_meteora_surface", "main"]

# Display constant, kept for consumers. The authoritative program id + PDA recipes
# now live in the packaged config (gecko/providers/configs/orquestra/meteora.json),
# loaded by build_meteora_surface — this is data, not code.
METEORA_PROGRAM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"


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


# The code half of the config: the multi-step plan callables this program exposes,
# keyed by intent name. The config lists the intent NAMES; here we supply their
# derivation logic. (Making plans declarative — data too — is a follow-on PR.)
METEORA_INTENTS: dict[str, Intent] = {_SWAP.name: _SWAP}


def build_meteora_surface() -> OrquestraProgramSurface:
    """Build the Meteora surface from packaged config (identity + PDA recipes) +
    the local intent registry (the plan callables)."""
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "meteora", METEORA_INTENTS)


def main(argv: list[str] | None = None) -> int:
    """DEPRECATED alias — use ``gecko-orquestra --program meteora``.

    Kept so the 0.9.2 command keeps working; delegates to the provider CLI with
    ``--program meteora`` prepended.
    """
    from .cli import main as _cli_main

    print(
        "meteora-demo is deprecated — use: gecko-orquestra --program meteora",
        file=sys.stderr,
    )
    passthrough = list(argv) if argv is not None else sys.argv[1:]
    return _cli_main(["--program", "meteora", *passthrough])
