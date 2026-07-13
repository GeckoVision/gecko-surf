"""Task 5.1 — probe/recorded → live is the SAME code path.

Flipping the mode runs the identical ``McpSurface.call_tool`` → ``client.call``
dispatch; the modes diverge ONLY at the transport edge (invariant #3):
  * ``live``     -> ``caller.execute`` (here, an INJECTED fake transport — no network);
  * ``probe``    -> the offline ``SimWorld`` sandbox (never reaches the wire);
  * ``recorded`` -> schema synthesis (never reaches the wire).

Light fake per repo test rules: one injected transport, no live call, no LLM, no keys.
"""

from __future__ import annotations

from typing import Any

from gecko.access import NoAuthSession
from gecko.client import AgentApiClient
from gecko.mcp_server import McpSurface
from gecko.modes import coerce_mode

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Pay API", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/balance": {
            "get": {
                "operationId": "getBalance",
                "summary": "Read the account balance.",
                "parameters": [
                    {
                        "name": "account",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}


def _surface(mode: str) -> tuple[McpSurface, list[Any]]:
    """One surface over a client with an INJECTED fake transport (no real network).
    A no-auth session keeps live from degrading (nothing to inject), so the live
    branch actually reaches the transport edge."""
    wire_calls: list[Any] = []

    def transport(req: Any) -> tuple[int, Any]:
        wire_calls.append(req)
        return 200, {"balance": 42}

    client = AgentApiClient(
        SPEC,
        base_url="https://api.example.com",
        session=NoAuthSession(),
        live_transport=transport,
    )
    return McpSurface(client, mode=mode, enforce="off"), wire_calls  # type: ignore[arg-type]


def test_live_reaches_the_transport_edge() -> None:
    surface, wire_calls = _surface("live")

    result = surface.call_tool("getBalance", {"account": "a"})

    assert wire_calls, "live must reach caller.execute (the injected transport)"
    assert result["mode"] == "live"
    assert result["status"] == 200


def test_probe_and_recorded_share_the_path_but_never_touch_the_wire() -> None:
    for mode in ("probe", "recorded"):
        surface, wire_calls = _surface(mode)

        result = surface.call_tool("getBalance", {"account": "a"})

        assert wire_calls == [], f"{mode} must never reach the transport edge"
        assert result["mode"] == mode


def test_only_the_transport_edge_differs_same_entrypoint() -> None:
    # The SAME McpSurface.call_tool method dispatches every mode; the divergence is the
    # transport edge alone. Probe answers with the sandbox's self-heal signals; live
    # answers from the injected transport. Neither changes the entrypoint.
    probe_surface, probe_wire = _surface("probe")
    live_surface, live_wire = _surface("live")

    probe = probe_surface.call_tool("getBalance", {})  # malformed: missing 'account'
    live = live_surface.call_tool("getBalance", {"account": "a"})

    # probe: SimWorld edge — a synthetic 422 + remediation, no wire
    assert probe_wire == []
    assert probe["status"] == 422
    assert "signals" in probe and "remediation" in probe
    # live: caller.execute edge — the injected transport answered
    assert live_wire and live["status"] == 200


def test_mode_flip_goes_through_effective_mode_and_coerce_mode() -> None:
    # The env/CLI boundary coerces a raw string; the client resolves the effective mode.
    assert coerce_mode("live") == "live"
    assert coerce_mode("bogus") == "recorded"  # fails closed to the $0 default

    surface, _ = _surface("live")
    client = surface.client
    # A no-auth, pinned surface stays live; probe/recorded pass through untouched.
    assert client._effective_mode("getBalance", "live") == "live"
    assert client._effective_mode("getBalance", "probe") == "probe"
    assert client._effective_mode("getBalance", "recorded") == "recorded"
