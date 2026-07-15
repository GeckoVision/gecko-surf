"""Jito served: READS live against mainnet, money-moving WRITES recorded (catalog-only).

The founder-confirmed boundary — *we are the catalog, not the relay*. Proven OFFLINE with
an injected transport that RAISES if the wire is ever touched: a sendBundle / sendTransaction
call must come back synthesized (recorded), never reaching the transport; a read must reach
it, prepared against the pinned mainnet Block Engine. The jito spec declares TWO servers, so
these also confirm the multi-server guard is satisfied by the explicit base_url pin (no
``AmbiguousServerError``).
"""

from __future__ import annotations

from typing import Any

from gecko.access import public_session
from gecko.caller import PreparedRequest
from gecko.client import AgentApiClient
from gecko.jito_surface import (
    JITO_MAINNET_BASE,
    JITO_SPEC_PATH,
    JITO_WRITE_OP_IDS,
    build_jito_surface,
    resolve_write_tool_names,
)
from gecko.mcp_server import McpSurface

WRITE_TOOL_NAMES = {"sendBundle", "sendTransaction"}


def _raise_on_wire(req: PreparedRequest) -> tuple[int, Any]:
    raise AssertionError(f"the wire must never be touched (got {req.url})")


def _live_jito(transport: Any) -> McpSurface:
    """A live jito surface with an INJECTED transport (so any wire touch is observable),
    built exactly like ``build_jito_surface`` but enforce=off to isolate the mode override
    from the risk gate."""
    client = AgentApiClient(
        str(JITO_SPEC_PATH),
        base_url=JITO_MAINNET_BASE,
        session=public_session(),
        live_transport=transport,
    )
    return McpSurface(
        client,
        mode="live",
        enforce="off",
        recorded_ops=resolve_write_tool_names(client),
    )


# --- the two money-movers resolve, and stay recorded (never hit the wire) --------------


def test_both_money_movers_resolve_to_tool_names() -> None:
    client = AgentApiClient(
        str(JITO_SPEC_PATH), base_url=JITO_MAINNET_BASE, session=public_session()
    )
    names = resolve_write_tool_names(client)
    assert names == WRITE_TOOL_NAMES
    assert len(names) == len(JITO_WRITE_OP_IDS)  # both resolved; none missing/collapsed


def test_sendBundle_stays_recorded_and_never_hits_the_wire() -> None:
    surface = _live_jito(_raise_on_wire)  # any wire touch explodes

    out = surface.call_tool("sendBundle", {"body": {"transactions": ["deadbeef"]}})

    assert out["mode"] == "recorded"  # synthesized, catalog-only
    assert "synthesized" in out["mode_note"].lower()  # the honest not-live note
    # No AssertionError from the transport == the wire was never touched.


def test_sendTransaction_stays_recorded_and_never_hits_the_wire() -> None:
    surface = _live_jito(_raise_on_wire)

    out = surface.call_tool("sendTransaction", {"body": {"transaction": "deadbeef"}})

    assert out["mode"] == "recorded"


# --- reads go live, prepared against the pinned mainnet host (no ambiguous-server) ------


def test_getTipAccounts_read_goes_live_to_mainnet() -> None:
    seen: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        seen.append(req.url)
        return 200, {"jsonrpc": "2.0", "result": ["tipacct"], "id": 1}

    surface = _live_jito(transport)
    out = surface.call_tool("getTipAccounts", {})

    assert out["mode"] == "live"  # wire path taken
    assert seen and seen[0].startswith(JITO_MAINNET_BASE)  # prepared against mainnet


def test_live_read_does_not_raise_ambiguous_server_error() -> None:
    # The spec declares mainnet + testnet; a live read would raise AmbiguousServerError if
    # the surface hadn't pinned an explicit base_url. It must resolve to mainnet instead.
    seen: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        seen.append(req.url)
        return 200, {"jsonrpc": "2.0", "result": [], "id": 1}

    surface = _live_jito(transport)
    surface.call_tool("getTipAccounts", {})  # would raise if the pin were missing
    assert seen and seen[0].startswith(JITO_MAINNET_BASE)


# --- the shared builder carries the whole boundary -------------------------------------


def test_build_jito_surface_is_live_with_recorded_writes_and_mainnet_pin() -> None:
    surface = build_jito_surface("block")

    assert isinstance(surface, McpSurface)
    assert surface.mode == "live"
    assert surface.enforce == "block"  # hosted risk gate preserved
    assert surface.recorded_ops == WRITE_TOOL_NAMES
    assert surface.client.base_url == JITO_MAINNET_BASE
    assert surface.client._base_url_explicit is True
    assert len(surface.client.servers) == 2  # multi-server spec, explicitly pinned


# --- the hosted serve_mcp host wires the split correctly -------------------------------


def test_serve_mcp_jito_surface_reads_live_writes_recorded() -> None:
    import gecko.serve_mcp as serve_mcp

    surfaces = dict(serve_mcp._build_surfaces(hosted_enforce="block"))
    jito = surfaces["jito"]

    assert isinstance(jito, McpSurface)
    assert jito.mode == "live"
    assert jito.enforce == "block"
    assert jito.client.base_url == JITO_MAINNET_BASE
    # recorded_ops resolved FROM the spec (not hardcoded) = exactly the two write ops.
    assert jito.recorded_ops == WRITE_TOOL_NAMES
