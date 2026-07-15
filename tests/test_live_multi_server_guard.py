"""Live mode on a multi-server spec fails closed — no silent ``servers[0]``.

The money-API footgun: a spec that declares production first and sandbox second
(e.g. Woovi) makes an un-pinned live call hit PRODUCTION silently. The fix: when
the caller never chose a ``base_url``, a live call on a >1-server spec raises a
typed ``AmbiguousServerError`` that lists every server and says how to choose.

Guard placement is the LIVE seam only — recorded/probe synthesis and the
quarantine live->recorded degradation ($0 flows) are byte-identical to before.
All offline: the wire is a fake transport (Pattern B).
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.access import public_session, stub_session
from gecko.caller import CallError, PreparedRequest
from gecko.client import AgentApiClient, AmbiguousServerError

PROD = "https://api.woovi.example"
SANDBOX = "https://api.woovi-sandbox.example"

# Woovi-shaped: production listed FIRST, sandbox second — the exact footgun order.
MULTI_SERVER_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Woovi-ish", "version": "1"},
    "servers": [
        {"url": PROD, "description": "Production"},
        {"url": SANDBOX, "description": "Sandbox"},
    ],
    "paths": {
        "/charges": {
            "get": {
                "operationId": "listCharges",
                "summary": "list charges",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}

SINGLE_SERVER_SPEC: dict[str, Any] = {
    **MULTI_SERVER_SPEC,
    "servers": [{"url": PROD}],
}


def _tool(client: AgentApiClient) -> str:
    return client.list_tools()[0]["name"]


def _no_wire(req: PreparedRequest) -> tuple[int, Any]:
    raise AssertionError(f"the wire must never be touched (got {req.url})")


# --- fail closed: live + multi-server + no explicit base_url -----------------


def test_live_call_on_multi_server_spec_without_base_url_fails_closed() -> None:
    # public_session: keyless live is the one flavor that reaches the wire on an
    # un-pinned dict spec (an auth session already degrades to recorded fail-closed).
    client = AgentApiClient(
        MULTI_SERVER_SPEC, session=public_session(), live_transport=_no_wire
    )
    with pytest.raises(AmbiguousServerError) as exc:
        client.call(_tool(client), {}, mode="live")
    msg = str(exc.value)
    # Lists BOTH servers with their index + description, and says how to fix it.
    assert "[0]" in msg and PROD in msg
    assert "[1]" in msg and SANDBOX in msg
    assert "Production" in msg and "Sandbox" in msg
    assert "2 servers" in msg
    assert "base_url" in msg and "--base-url" in msg


def test_ambiguous_server_error_is_a_typed_call_error() -> None:
    # One error family for the whole get->prepare->call path: existing
    # ``except CallError`` handlers (MCP surface, testgen) surface it helpfully.
    assert issubclass(AmbiguousServerError, CallError)


def test_error_omits_descriptions_the_spec_does_not_provide() -> None:
    spec = {
        **MULTI_SERVER_SPEC,
        "servers": [{"url": PROD}, {"url": SANDBOX}],
    }
    client = AgentApiClient(spec, session=public_session(), live_transport=_no_wire)
    with pytest.raises(AmbiguousServerError) as exc:
        client.call(_tool(client), {}, mode="live")
    msg = str(exc.value)
    assert PROD in msg and SANDBOX in msg
    assert "(" not in msg.split("—", 1)[1].rsplit("—", 1)[0]  # no invented labels


# --- recorded mode is completely unaffected ----------------------------------


def test_recorded_call_on_multi_server_spec_is_unchanged() -> None:
    client = AgentApiClient(MULTI_SERVER_SPEC, live_transport=_no_wire)
    result = client.call(_tool(client), {})  # default mode="recorded"
    assert result["mode"] == "recorded"
    assert result["status"] == 200
    # Recorded synthesis still uses the servers[0] default for the templated URL.
    assert result["request"].startswith(PROD)


def test_quarantine_degradation_to_recorded_still_wins_over_the_guard() -> None:
    # An auth-carrying session on an unverified surface degrades live->recorded
    # (fail closed, $0). That degraded flow must NOT gain new friction: the guard
    # fires only when the wire would actually be hit.
    client = AgentApiClient(
        MULTI_SERVER_SPEC, session=stub_session(), live_transport=_no_wire
    )
    result = client.call(_tool(client), {}, mode="live")
    assert result["mode"] == "recorded"


# --- explicit base_url: live proceeds (the hosted/serve construction) --------


def test_explicit_base_url_on_multi_server_spec_live_calls_fine() -> None:
    seen: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        seen.append(req.url)
        return 200, {"ok": True}

    client = AgentApiClient(
        MULTI_SERVER_SPEC, base_url=SANDBOX, live_transport=transport
    )
    result = client.call(_tool(client), {}, mode="live")
    assert result["status"] == 200
    assert result["mode"] == "live"
    assert seen and seen[0].startswith(SANDBOX)


# --- single-server spec: unchanged --------------------------------------------


def test_single_server_spec_live_call_is_unchanged() -> None:
    def transport(req: PreparedRequest) -> tuple[int, Any]:
        return 200, {"ok": True}

    client = AgentApiClient(
        SINGLE_SERVER_SPEC, session=public_session(), live_transport=transport
    )
    result = client.call(_tool(client), {}, mode="live")
    assert result["status"] == 200
    assert result["mode"] == "live"


# --- hosted provider surface pins the multi-server jito spec explicitly ------


def test_serve_providers_pins_jito_base_url_and_records_money_movers() -> None:
    # Confirm (not assume) the hosted path: the jito block-engine spec declares
    # mainnet+testnet, and the provider host serves its READS live — so its client must
    # carry an explicit base_url or every hosted read would fail closed. The two
    # money-moving writes stay RECORDED (catalog-only), never relayed.
    from gecko import serve_providers
    from gecko.jito_surface import JITO_MAINNET_BASE
    from gecko.mcp_server import McpSurface

    surfaces = dict(serve_providers._build_surfaces())
    jito = surfaces["jito"]
    assert isinstance(jito, McpSurface)
    assert jito.mode == "live"
    client = jito.client
    assert len(client.servers) == 2
    assert client.base_url == JITO_MAINNET_BASE
    assert client._base_url_explicit is True
    # catalog, not relay: the money-movers are recorded even on the live surface.
    assert jito.recorded_ops == {"sendBundle", "sendTransaction"}
