"""The hosted ORE Program Surface — comprehended from the bundled source, served at /ore.

ORE is a Steel program (no Anchor IDL); its PDA seed recipes ship as a bundled source
slice. This proves the bundled surface derives the real mainnet addresses, that the
hosted multi-surface server mounts it (public, ungated), and that /ore/mcp actually
serves derive_pda end-to-end over the wire (in-process ASGI — no socket, $0).
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

from gecko.examples.ore import ORE_PROGRAM_ID, build_ore_surface

mcp = pytest.importorskip("mcp")  # skip cleanly without the serve extra

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko.http_server import build_http_app  # noqa: E402

ORE_CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"
ORE_TREASURY = "45db2FSR4mcXdSVVZbKbwojU6uYDpMyhpEi7cC8nHaWG"
ORE_BOARD = "BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi"
BASE = "http://ore.test"


# --- the bundled surface comprehends the real recipes -----------------------


def test_bundled_ore_surface_derives_mainnet_ground_truth() -> None:
    surface = build_ore_surface()
    graph = surface.call_tool("get_program_graph", {})
    assert graph["program_id"] == ORE_PROGRAM_ID
    assert len(graph["pdas"]) == 7  # the seeds an IDL/llms.txt carries none of
    for account, expected in (
        ("config", ORE_CONFIG),
        ("treasury", ORE_TREASURY),
        ("board", ORE_BOARD),
    ):
        out = surface.call_tool("derive_pda", {"account": account})
        assert out["address"] == expected


# --- the hosted server mounts it, public + ungated --------------------------


def test_hosted_server_mounts_ore_ungated() -> None:
    from gecko.serve_mcp import GATED_SURFACES, _build_surfaces

    surfaces = _build_surfaces("block")
    names = {name for name, _ in surfaces}
    assert "ore" in names  # the hosted server mounts it
    assert "ore" not in GATED_SURFACES  # a public demo surface, no key required


# --- /ore/mcp serves derive_pda end-to-end (in-process ASGI) ----------------


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


def _call(app: Any, name: str, args: dict[str, Any]) -> str:
    async def body(session: ClientSession) -> str:
        res = await session.call_tool(name, args)
        return res.content[0].text  # type: ignore[union-attr]

    return anyio.run(_connect, app, body)


def _tool_names(app: Any) -> set[str]:
    async def body(session: ClientSession) -> set[str]:
        res = await session.list_tools()
        return {t.name for t in res.tools}

    return anyio.run(_connect, app, body)


def test_ore_mcp_serves_the_three_tools() -> None:
    app = build_http_app(
        build_ore_surface(), server_name="ore", allowed_hosts=["ore.test"]
    )
    assert {"get_program_graph", "plan_instruction", "derive_pda"} <= _tool_names(app)


def test_ore_mcp_derive_pda_over_the_wire() -> None:
    app = build_http_app(
        build_ore_surface(), server_name="ore", allowed_hosts=["ore.test"]
    )
    out = json.loads(_call(app, "derive_pda", {"account": "config"}))
    assert out["address"] == ORE_CONFIG
