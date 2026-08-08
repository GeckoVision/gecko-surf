"""An upstream failure must reach the agent AS a failure (MCP ``isError``).

The bug this pins: a live 404 came back as ``isError: false`` with an empty ``data``,
so an agent read a dead endpoint as a successful call with no results — a silent wrong
answer inside a product whose whole claim is first-call-correct.

The decision lives in the package (``gecko.toolerror``); both MCP transports
(Streamable HTTP + stdio) are thin and share it, so no surface can drift.
"""

from __future__ import annotations

from typing import Any

import anyio
import httpx
import pytest

from gecko.access import public_session
from gecko.toolerror import is_upstream_failure, tool_result_payload

mcp = pytest.importorskip("mcp")  # skip cleanly without the serve extra

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko.http_server import build_http_app  # noqa: E402

BASE = "http://err.test"


# --- the decision function (one source of truth) -----------------------------


def test_a_2xx_live_result_is_not_a_failure() -> None:
    assert (
        is_upstream_failure({"status": 200, "data": {"ok": True}, "mode": "live"})
        is False
    )


def test_a_recorded_result_is_not_a_failure() -> None:
    assert is_upstream_failure({"status": 200, "data": {}, "mode": "recorded"}) is False


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
def test_any_upstream_4xx_or_5xx_is_a_failure(status: int) -> None:
    assert is_upstream_failure({"status": status, "data": "", "mode": "live"}) is True


def test_a_structured_error_payload_is_a_failure() -> None:
    # The program surfaces (ore/meteora/...) answer an unknown instruction with an
    # `error` key and no HTTP status — same silent-success symptom, same fix.
    assert (
        is_upstream_failure({"error": "no instruction 'mine'", "available": []}) is True
    )


def test_a_blocked_call_is_a_failure() -> None:
    # A refused call never executed; the agent must not read the refusal as a result.
    assert (
        is_upstream_failure({"blocked": True, "decision": "block", "reasons": []})
        is True
    )


def test_an_empty_error_key_is_not_a_failure() -> None:
    # `error: null` is how several APIs say "no error" — don't invent a failure.
    assert is_upstream_failure({"status": 200, "error": None}) is False


def test_a_non_dict_result_is_not_a_failure() -> None:
    assert is_upstream_failure([{"name": "a"}]) is False  # search_capabilities hits
    assert is_upstream_failure("ok") is False


def test_payload_keeps_the_whole_body_so_the_agent_can_self_heal() -> None:
    text, is_error = tool_result_payload({"status": 404, "request": "https://h/x"})
    assert is_error is True
    assert "404" in text and "https://h/x" in text  # body preserved, not swallowed


# --- the transport actually carries it to the agent --------------------------


SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "probe api", "version": "1"},
    "servers": [{"url": "https://api.test"}],
    "paths": {
        "/probe": {
            "get": {
                "operationId": "probe",
                "summary": "probe the API",
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    }
                },
            }
        }
    },
}


def _call_over_the_wire(status: int, payload: Any) -> Any:
    """One real live call through the real surface, with the WIRE faked (no network)."""
    from gecko.client import AgentApiClient
    from gecko.mcp_server import McpSurface

    def transport(_req: Any) -> tuple[int, Any]:
        return status, payload

    client = AgentApiClient(
        SPEC,
        base_url="https://api.test",
        session=public_session(),
        live_transport=transport,
    )
    app = build_http_app(
        McpSurface(client, mode="live", enforce="off"),
        server_name="err",
        allowed_hosts=["err.test"],
    )

    async def ask(session: ClientSession) -> Any:
        return await session.call_tool("probe", {})

    async def run() -> Any:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http_client:
                async with streamable_http_client(
                    f"{BASE}/mcp", http_client=http_client
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await ask(session)

    return anyio.run(run)


def test_a_live_404_reaches_the_agent_as_an_error() -> None:
    res = _call_over_the_wire(404, "")

    assert res.isError is True
    assert "404" in res.content[0].text  # the body still reaches the agent


def test_a_live_200_still_reaches_the_agent_as_a_success() -> None:
    res = _call_over_the_wire(200, {"a": 1})

    assert res.isError is False


# --- the same fix covers the PROGRAM surfaces (no HTTP status at all) ---------


def test_a_program_surface_error_reaches_the_agent_as_an_error() -> None:
    """ORE's ``plan_instruction`` answers an unknown instruction with ``{"error": ...}``
    and no status — it showed the identical silent-success symptom before the shared fix."""
    from gecko.examples.ore import build_ore_surface

    app = build_http_app(
        build_ore_surface(), server_name="ore", allowed_hosts=["err.test"]
    )

    async def ask(session: ClientSession) -> Any:
        return await session.call_tool("plan_instruction", {"instruction": "nope"})

    async def run() -> Any:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http_client:
                async with streamable_http_client(
                    f"{BASE}/mcp", http_client=http_client
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await ask(session)

    res = anyio.run(run)
    assert res.isError is True
    assert "no instruction" in res.content[0].text
