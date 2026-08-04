"""Phase 1: the Meteora Orquestra provider surface — the touchable demo.

The agent plans a swap in plain English; Gecko derives lb_pair (the root Orquestra can't)
+ the leaves, and the plan points at Orquestra's builder. Proves the derivation matches
the real mainnet pool and the plan carries the right execute target.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from gecko.providers.meteora import (
    METEORA_PROGRAM_ID,
    build_meteora_surface,
)

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Ground truth — a REAL, CURRENT (post-May-2024) mainnet DLMM pool, created with the
# `derive_lb_pair_pda2` scheme (`require_base_factor_seed == 1` on-chain). Values decoded
# from the pool's own LbPair account (mints at offsets 88/120, bin_step u16 @80, the
# base_factor_seed u16 @84) and cross-checked against MeteoraAg/dlmm-sdk's own fixture.
# This is the pool the OLD 3-seed recipe derives WRONG — the whole point of the fix.
TOKEN = "Df6yfrKC8kZE3KNkrHERKzAetSxbrWeniQfyJY4Jpump"  # token_x of the pool
BIN_STEP = 250
BASE_FACTOR = 4000
CURRENT_POOL = "EtAdVRLFH22rjWh3mcUasKFF27WtHhsaCvK27tPFFWig"

# The pre-upgrade survivor: a 3-seed (`require_base_factor_seed == 0`) SOL/USDC pool that
# predates PR #49. It is NOT derivable by today's 4-seed recipe — it is exactly why the
# base_factor bug stayed invisible in our old tests (see test_legacy_pool_* below).
LEGACY_SOL_USDC = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


def test_surface_lists_derive_and_swap_intent() -> None:
    tools = {t["name"] for t in build_meteora_surface().list_tools()}
    assert {"get_program_graph", "derive_pda", "plan_swap"} <= tools


def test_derive_pda_gives_the_real_current_pool() -> None:
    s = build_meteora_surface()
    out = s.call_tool(
        "derive_pda",
        {
            "account": "lb_pair",
            "bindings": {
                "token_x_mint": TOKEN,
                "token_y_mint": WSOL,
                "bin_step": BIN_STEP,
                "base_factor": BASE_FACTOR,
            },
        },
    )
    assert out["address"] == CURRENT_POOL


def test_base_factor_is_load_bearing_old_recipe_derived_wrong_pool() -> None:
    # The differential guard: the deprecated 3-seed scheme derives a DIFFERENT address for
    # this current pool (the silent wrong-pool bug), while the shipped 4-seed scheme lands
    # it. If someone ever drops base_factor again, this test fails instead of the bug
    # shipping silently.
    from gecko.pda import PdaNode, derive_pda
    from gecko.provider_config import node_from_spec

    seeds = [
        {"kind": "ordered_pair", "left": "x", "right": "y", "select": "min"},
        {"kind": "ordered_pair", "left": "x", "right": "y", "select": "max"},
        {
            "kind": "variable",
            "name": "bin_step",
            "source": "argument",
            "encoding": "le",
            "width": 2,
        },
    ]
    bindings = {"x": TOKEN, "y": WSOL, "bin_step": BIN_STEP, "base_factor": BASE_FACTOR}
    old_node: PdaNode = node_from_spec(
        "lb_pair_legacy", {"program_id": METEORA_PROGRAM_ID, "seeds": seeds}
    )
    old_addr = derive_pda(old_node, bindings).address
    assert old_addr != CURRENT_POOL  # the bug: old recipe → wrong pool

    new_node = node_from_spec(
        "lb_pair",
        {
            "program_id": METEORA_PROGRAM_ID,
            "seeds": [
                *seeds,
                {
                    "kind": "variable",
                    "name": "base_factor",
                    "source": "argument",
                    "encoding": "le",
                    "width": 2,
                },
            ],
        },
    )
    assert derive_pda(new_node, bindings).address == CURRENT_POOL


def test_legacy_pool_needs_the_deprecated_3seed_scheme() -> None:
    # Honesty: the pre-upgrade SOL/USDC pool used the 3-seed `derive_lb_pair_pda`
    # (require_base_factor_seed == 0) and therefore CANNOT be reproduced by the shipped
    # 4-seed recipe with any base_factor — it is a legacy artifact, not a current pool.
    from gecko.pda import derive_pda
    from gecko.provider_config import node_from_spec

    three_seed = node_from_spec(
        "lb_pair_legacy",
        {
            "program_id": METEORA_PROGRAM_ID,
            "seeds": [
                {"kind": "ordered_pair", "left": "x", "right": "y", "select": "min"},
                {"kind": "ordered_pair", "left": "x", "right": "y", "select": "max"},
                {
                    "kind": "variable",
                    "name": "bin_step",
                    "source": "argument",
                    "encoding": "le",
                    "width": 2,
                },
            ],
        },
    )
    addr = derive_pda(three_seed, {"x": WSOL, "y": USDC, "bin_step": 4}).address
    assert addr == LEGACY_SOL_USDC  # the deprecated scheme still derives its own pool


def test_plan_swap_derives_the_full_set_and_points_at_orquestra() -> None:
    s = build_meteora_surface()
    plan = s.call_tool(
        "plan_swap",
        {
            "input_mint": WSOL,
            "output_mint": TOKEN,
            "bin_step": str(BIN_STEP),
            "base_factor": str(BASE_FACTOR),
        },
    )
    assert plan["instruction"] == "swap"
    assert (
        plan["derived"]["lb_pair"] == CURRENT_POOL
    )  # the root Orquestra returns "no seeds" for
    assert plan["derived"]["reserve_x"] and plan["derived"]["oracle"]
    # the plan points at Orquestra's builder — we don't proxy it
    ex = plan["execute"]
    assert ex["method"] == "POST"
    assert ex["url"] == (
        "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl/instructions/swap/build"
    )


def test_plan_swap_missing_base_factor_is_reported() -> None:
    # base_factor is now a required input (the fee tier); omitting it must be an honest
    # error, not a silently-wrong pool.
    plan = build_meteora_surface().call_tool(
        "plan_swap", {"input_mint": WSOL, "output_mint": TOKEN, "bin_step": "250"}
    )
    assert "error" in plan and "base_factor" in str(plan)


def test_surface_identity_comes_from_config() -> None:
    # the config-driven backbone: identity + project base come from packaged config,
    # not hardcoded literals in meteora.py — proven end to end by the real derivation.
    surface = build_meteora_surface()
    assert surface.program_id == METEORA_PROGRAM_ID
    assert (
        surface.project_base_url
        == "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl"
    )
    assert (
        surface.derive(
            "lb_pair",
            {
                "token_x_mint": TOKEN,
                "token_y_mint": WSOL,
                "bin_step": BIN_STEP,
                "base_factor": BASE_FACTOR,
            },
        )
        == CURRENT_POOL
    )


def test_get_program_graph_summarizes_pdas_and_intents() -> None:
    g = build_meteora_surface().call_tool("get_program_graph", {})
    assert g["program_id"] == METEORA_PROGRAM_ID
    assert "lb_pair" in g["pdas"]
    assert g["pdas"]["lb_pair"]["needs"] == [
        "token_x_mint",
        "token_y_mint",
        "bin_step",
        "base_factor",
    ]
    assert "plan_swap" in g["intents"]


@pytest.mark.skipif(
    os.environ.get("GECKO_SURFPOOL_E2E") != "1",
    reason="on-chain gate: set GECKO_SURFPOOL_E2E=1 with a surfpool mainnet fork",
)
def test_current_pool_holds_a_real_account_on_fork() -> None:
    # Strongest gate ($0, no key): the 4-seed recipe derives an address that actually
    # holds the live Meteora account on a mainnet fork. Reads owner/existence only —
    # never the payload (control-plane invariant #1).
    from gecko.pda_testkit import verify_derivation

    node = build_meteora_surface().pdas["lb_pair"]
    rpc_url = os.environ.get("GECKO_SURFPOOL_RPC", "http://127.0.0.1:8899")
    check = verify_derivation(
        node,
        {
            "token_x_mint": TOKEN,
            "token_y_mint": WSOL,
            "bin_step": BIN_STEP,
            "base_factor": BASE_FACTOR,
        },
        rpc_url=rpc_url,
    )
    assert check.address == CURRENT_POOL
    assert check.exists and check.owner_matches


# --- served over the wire (in-process ASGI, so Berkay's `claude mcp add` works) ---

mcp = pytest.importorskip("mcp")

import anyio  # noqa: E402
import httpx  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko.http_server import build_http_app  # noqa: E402

BASE = "http://meteora.test"


async def _connect(app: Any, fn: Any) -> Any:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as http_client:
            async with streamable_http_client(
                f"{BASE}/mcp", http_client=http_client
            ) as (
                read,
                write,
                _sid,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)


def test_plan_swap_over_mcp() -> None:
    app = build_http_app(
        build_meteora_surface(), server_name="meteora", allowed_hosts=["meteora.test"]
    )

    async def body(session: ClientSession) -> str:
        res = await session.call_tool(
            "plan_swap",
            {
                "input_mint": WSOL,
                "output_mint": TOKEN,
                "bin_step": str(BIN_STEP),
                "base_factor": str(BASE_FACTOR),
            },
        )
        return res.content[0].text  # type: ignore[union-attr]

    plan = json.loads(anyio.run(_connect, app, body))
    assert plan["derived"]["lb_pair"] == CURRENT_POOL
