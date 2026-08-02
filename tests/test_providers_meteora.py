"""Phase 1: the Meteora Orquestra provider surface — the touchable demo.

The agent plans a swap in plain English; Gecko derives lb_pair (the root Orquestra can't)
+ the leaves, and the plan points at Orquestra's builder. Proves the derivation matches
the real mainnet pool and the plan carries the right execute target.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko.providers.meteora import (
    METEORA_PROGRAM_ID,
    build_meteora_surface,
)

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LB_PAIR = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"  # real mainnet SOL/USDC pool


def test_surface_lists_derive_and_swap_intent() -> None:
    tools = {t["name"] for t in build_meteora_surface().list_tools()}
    assert {"get_program_graph", "derive_pda", "plan_swap"} <= tools


def test_derive_pda_gives_the_real_pool() -> None:
    s = build_meteora_surface()
    out = s.call_tool(
        "derive_pda",
        {
            "account": "lb_pair",
            "bindings": {"token_x_mint": WSOL, "token_y_mint": USDC, "bin_step": 4},
        },
    )
    assert out["address"] == LB_PAIR


def test_plan_swap_derives_the_full_set_and_points_at_orquestra() -> None:
    s = build_meteora_surface()
    plan = s.call_tool(
        "plan_swap", {"input_mint": WSOL, "output_mint": USDC, "bin_step": "4"}
    )
    assert plan["instruction"] == "swap"
    assert (
        plan["derived"]["lb_pair"] == LB_PAIR
    )  # the root Orquestra returns "no seeds" for
    assert plan["derived"]["reserve_x"] and plan["derived"]["oracle"]
    # the plan points at Orquestra's builder — we don't proxy it
    ex = plan["execute"]
    assert ex["method"] == "POST"
    assert ex["url"] == (
        "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl/instructions/swap/build"
    )


def test_plan_swap_missing_input_is_reported() -> None:
    plan = build_meteora_surface().call_tool("plan_swap", {"input_mint": WSOL})
    assert "error" in plan and "bin_step" in plan["error"]


def test_surface_identity_comes_from_config() -> None:
    # the config-driven backbone: identity + project base come from packaged config,
    # not hardcoded literals in meteora.py — proven end to end by the real derivation.
    surface = build_meteora_surface()
    assert surface.program_id == METEORA_PROGRAM_ID
    assert surface.project_base_url == "https://api.orquestra.dev/api/v48gsz901w84zriqe0elsl"
    assert surface.derive("lb_pair", {"token_x_mint": WSOL, "token_y_mint": USDC, "bin_step": 4}) == LB_PAIR


def test_get_program_graph_summarizes_pdas_and_intents() -> None:
    g = build_meteora_surface().call_tool("get_program_graph", {})
    assert g["program_id"] == METEORA_PROGRAM_ID
    assert "lb_pair" in g["pdas"]
    assert g["pdas"]["lb_pair"]["needs"] == ["token_x_mint", "token_y_mint", "bin_step"]
    assert "plan_swap" in g["intents"]


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
            "plan_swap", {"input_mint": WSOL, "output_mint": USDC, "bin_step": "4"}
        )
        return res.content[0].text  # type: ignore[union-attr]

    plan = json.loads(anyio.run(_connect, app, body))
    assert plan["derived"]["lb_pair"] == LB_PAIR
