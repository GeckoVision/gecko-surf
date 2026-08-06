"""Jupiter — the program surface, and the honest statement of what it cannot carry.

The landing orchestrator lives in :mod:`gecko.providers.jupiter_landing`. This module is
the *surface* half: the intent an agent routes to, and the declaration of where each
account comes from — which for this program is the whole point.

`route` declares **9 accounts and 0 PDA-derivable accounts**. The instruction that lands
carries **25 and an address lookup table**. The missing 16 are the AMM legs of whichever
route the aggregator's HTTP quote chose at that instant, so they are `cross_surface`: not
extracted (the program never declared them), not recovered (no source derives them), and
not flagged (we know exactly what they are). They simply belong to a different surface.

This is why the two Gecko lanes are one product. An agent holding a perfect IDL *and* a
perfect OpenAPI spec still cannot make this call, because the joining fact lives between
them.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..find_start import StartSpec
from .orquestra import Intent, OrquestraProgramSurface
from .jupiter_landing import DECLARED_ROUTE_ACCOUNTS, plan_route

__all__ = ["JUPITER_INTENTS", "JUPITER_STARTS", "build_jupiter_surface"]


def _route_plan(
    surface: OrquestraProgramSurface, args: Mapping[str, Any]
) -> dict[str, str]:
    """Intent adapter for the MCP surface: plan the route and hand back the accounts.

    The plan's full detail — the route legs, the price impact, the lookup table — comes
    from the standalone :func:`plan_route`; this returns the named account map the
    surface wraps with the builder's execute URL.
    """
    plan = plan_route(args)
    return {
        f"account_{index}": account.pubkey
        for index, account in enumerate(plan["accounts"])
    }


_ROUTE = Intent(
    name="plan_route",
    instruction="route",
    description=(
        "Plan a Jupiter swap route — the aggregator path for exiting or entering a "
        "position. Give input_mint, output_mint, amount and user; Gecko asks the "
        "aggregator's quote surface which venues can absorb the trade right now, "
        "assembles the complete account set for the route instruction, and tags every "
        "account with where it came from. The program surface declares 9 accounts; the "
        "instruction that lands carries 25 plus an address lookup table. The other 16 "
        "are the AMM legs of the chosen route and exist in NO IDL — they are facts "
        "about a route an HTTP API computed a second ago, not facts about the program. "
        "Use this to exit a depegging asset, rebalance, or take a quoted price with "
        "measured slippage rather than an assumed one."
    ),
    inputs=("input_mint", "output_mint", "amount", "user"),
    plan=_route_plan,
)

#: The code half of the config: the plan callables this program exposes, keyed by intent.
JUPITER_INTENTS: dict[str, Intent] = {_ROUTE.name: _ROUTE}

#: What find_start declares about plan_route. The `recovered` map carries the one seed
#: the surface drops; the cross-surface accounts are deliberately NOT listed as derivable
#: — they cannot be known before the quote, and promising them here would be a lie the
#: derive plan then has to break.
JUPITER_STARTS: dict[str, StartSpec] = {
    "plan_route": StartSpec(
        accounts=tuple(DECLARED_ROUTE_ACCOUNTS),
        recovered={
            "event_authority": (
                "Anchor's event-authority PDA. Orquestra reports `pda: null` for it — "
                "the seed is a const in program source, not an IDL field (the #4057 "
                'class). Recovered recipe: seeds ["__event_authority"].'
            ),
        },
        preludes=(),
        gaps=(),
    ),
}


def build_jupiter_surface() -> OrquestraProgramSurface:
    """Build the Jupiter surface from packaged config + the local intent registry."""
    from .cli import build_surface_from_config

    return build_surface_from_config("orquestra", "jupiter", JUPITER_INTENTS)
