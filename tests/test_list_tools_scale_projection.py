"""list_tools scale projection — make the O(1)-at-scale token claim TRUE.

Below scale, ``McpSurface.list_tools`` is BYTE-IDENTICAL to today (all current hosted
surfaces are <50 ops, so they MUST be unaffected). Above scale it returns lightweight
references (name + one-line summary + minimal valid ``inputSchema``) that tell the agent
to fetch the full schema via ``search_capabilities`` — which stays a full callable tool
and now returns full callable defs. The projection only hides schemas from the *list*; it
must never make a real tool uncallable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko.client import AgentApiClient
from gecko.mcp_server import (
    _QUERY_DOCS_TOOL,
    _SEARCH_TOOL,
    McpSurface,
    to_lightweight_ref,
)

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _big_spec(n: int = 120) -> dict[str, Any]:
    """A synthetic OpenAPI spec with ``n`` no-auth GET ops — above the scale threshold (>50).

    Each op has a distinctive summary (so search can match it), a JSON response schema (so
    recorded-mode calls synthesize a 200), and several OPTIONAL query params with descriptions
    — a realistic painful-API parameter schema. Optional (not required) so ``call_tool(name,
    {})`` still succeeds; substantial enough that withholding it from the list actually saves
    tokens (the real-world case the projection targets)."""
    paths: dict[str, Any] = {}
    for i in range(n):
        params = [
            {
                "name": pname,
                "in": "query",
                "required": False,
                "description": (
                    f"Optional {pname} filter applied when retrieving widget kind {i}; "
                    f"accepts a canonical {pname} value and narrows the result set."
                ),
                "schema": {"type": "string"},
            }
            for pname in ("region", "since", "until", "status", "cursor", "limit")
        ]
        paths[f"/widgets/kind_{i}"] = {
            "get": {
                "operationId": f"getWidgetKind{i}",
                "summary": f"Retrieve widget metadata for kind {i}",
                "description": f"Returns the widget record for catalog kind {i}.",
                "parameters": params,
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "name": {"type": "string"},
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Big Widget API", "version": "1.0.0"},
        "paths": paths,
    }


def _tokens(enc: Any, defs: list[dict[str, Any]]) -> int:
    return len(enc.encode(json.dumps(defs, separators=(",", ":"))))


def _todays_full_list(client: AgentApiClient) -> list[dict[str, Any]]:
    """Reconstruct exactly what list_tools emitted before the projection existed:
    the synthetic tools followed by a full {name, description, inputSchema} per usable tool."""
    tools = [_SEARCH_TOOL, _QUERY_DOCS_TOOL]
    for t in client.list_tools():
        tools.append({k: t[k] for k in ("name", "description", "inputSchema")})
    return tools


# --- below scale: byte-identical to today (guards current hosted surfaces) -------------


def test_below_scale_list_tools_is_byte_identical_to_today():
    client = AgentApiClient(str(FIXTURE))
    assert client.surface_all is True  # 18 ops -> below scale
    surface = McpSurface(client)
    assert surface.list_tools() == _todays_full_list(client)


def test_below_scale_first_tool_is_full_search_and_rest_are_full_defs():
    client = AgentApiClient(str(FIXTURE))
    tools = McpSurface(client).list_tools()
    assert tools[0] == _SEARCH_TOOL
    assert tools[1] == _QUERY_DOCS_TOOL
    assert len(tools) == 20  # 2 synthetic (search + query_docs) + 18 endpoints
    # full defs carry the real parameter schema (properties), not a stub
    non_search = [t for t in tools[2:]]
    assert all("properties" in t["inputSchema"] for t in non_search)


# --- above scale: lightweight refs + full search tool ----------------------------------


def test_above_scale_returns_lightweight_refs_plus_full_search():
    client = AgentApiClient(_big_spec(120))
    assert client.surface_all is False  # 120 ops -> above scale
    tools = McpSurface(client).list_tools()

    assert tools[0] == _SEARCH_TOOL  # search stays a full callable tool
    assert tools[1] == _QUERY_DOCS_TOOL  # query_docs stays a full callable tool
    refs = tools[2:]
    assert len(refs) == 120
    for ref in refs:
        assert set(ref.keys()) == {"name", "description", "inputSchema"}
        assert ref["description"].endswith(
            "call search_capabilities for the full schema"
        )
        # minimal valid MCP inputSchema — no parameter schema leaked into the list
        assert ref["inputSchema"] == {"type": "object"}
        assert "properties" not in ref["inputSchema"]


def test_lightweight_ref_is_control_plane_safe():
    client = AgentApiClient(_big_spec(120))
    full = client.list_tools()[0]
    ref = to_lightweight_ref(full)
    # carries only name + summary + minimal schema — no auth, no _invoke, no payload
    assert set(ref.keys()) == {"name", "description", "inputSchema"}
    assert ref["name"] == full["name"]
    assert "requires_auth" not in ref
    assert "auth_schemes" not in ref
    assert "_invoke" not in ref


# --- above scale: still discoverable + callable (projection never hides a tool) --------


def test_above_scale_tool_stays_discoverable_and_callable():
    client = AgentApiClient(_big_spec(120))
    surface = McpSurface(client)

    listed = surface.list_tools()
    ref_names = {t["name"] for t in listed[1:]}
    target = "getWidgetKind77"
    assert target in ref_names  # present in the list only as a lightweight ref

    # search_capabilities returns the FULL callable def (with inputSchema) for the op
    hits = surface.call_tool(
        "search_capabilities", {"query": "widget metadata for kind 77"}
    )
    match = next((h for h in hits if h["name"] == target), None)
    assert match is not None
    assert "inputSchema" in match and match["inputSchema"].get("type") == "object"

    # and it is callable by name in recorded mode — the projection hid the schema
    # from the list, never made the tool uncallable
    result = surface.call_tool(target, {})
    assert result["status"] == 200
    assert result["mode"] == "recorded"


# --- the honest proof: measured token reduction on the >=120-op fixture ----------------


def test_above_scale_token_reduction_is_substantial():
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("cl100k_base")
    client = AgentApiClient(_big_spec(120))
    full_dump = _todays_full_list(client)  # what today would emit above scale
    projected = McpSurface(client).list_tools()

    full_tokens = _tokens(enc, full_dump)
    proj_tokens = _tokens(enc, projected)
    reduction = 1 - proj_tokens / full_tokens

    # Report the real numbers — this is the claim being made true.
    print(
        f"\n[list_tools @120 ops] full-dump={full_tokens} tok  "
        f"projected={proj_tokens} tok  reduction={reduction:.1%}"
    )
    assert proj_tokens < full_tokens * 0.5  # a substantial, measured cut
