"""`get_surface_graph` — the hidden, opt-in, SCOPED agent door to the call graph.

Callable by name (like `get_capability` / `query_docs`) but NOT enumerated in
`list_tools`, so the strict projection tests stay byte-identical. Bounded output:
an `op` returns that op's neighbors + plan; no `op` returns a summary (no full edge
list). Control-plane clean — structure only, never a payload or secret.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gecko.client import AgentApiClient
from gecko.mcp_server import McpSurface

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _surface() -> McpSurface:
    return McpSurface(AgentApiClient(str(FIXTURE)))


def test_scoped_by_op_returns_neighbors_and_plan() -> None:
    # getApiFixturesValidation consumes fixtureId, which getApiFixturesSnapshot supplies.
    op = "getApiFixturesValidation"
    result = _surface().call_tool("get_surface_graph", {"op": op})

    assert result["op"] == op
    # Every returned edge touches ONLY the requested op (its suppliers + consumers).
    assert result["edges"], "expected at least one neighbor edge"
    for edge in result["edges"]:
        assert edge["from"] == op or edge["to"] == op
    # The ordered supplier chain rides along (reused from client.plan_for).
    assert "plan" in result
    step_ops = [s["operation_id"] for s in result["plan"]["steps"]]
    assert "getApiFixturesSnapshot" in step_ops  # the supplier of fixtureId
    assert result["summary"]["edges"] == len(result["edges"])


def test_summary_projection_drops_the_full_edge_list() -> None:
    result = _surface().call_tool("get_surface_graph", {})

    # The operation list is present (id / method / path), bounded and structure-only.
    assert result["operations"]
    for op in result["operations"]:
        assert set(op) == {"id", "method", "path"}
    # The whole-graph edge list is deliberately NOT dumped — named honestly instead.
    assert isinstance(result["edges"], str)
    assert "get_surface_graph(op=" in result["edges"]
    # Summary counts still report the real edge total without paying its token cost.
    assert result["summary"]["operations"] == len(result["operations"])
    assert result["summary"]["edges"] > 0


def test_get_surface_graph_is_hidden_from_list_tools() -> None:
    surface = _surface()
    listed = surface.list_tools()
    names = {t["name"] for t in listed}
    assert "get_surface_graph" not in names  # callable by name, never enumerated
    # The projection is untouched: same shape the strict tests hard-code.
    assert listed[0]["name"] == "search_capabilities"
    assert listed[1]["name"] == "query_docs"
    assert len(listed) == 20


def test_control_plane_clean_no_auth_or_payload() -> None:
    # The txodds surface injects an Authorization: Bearer header + an X-Api-Token header
    # at CALL time. The graph is structure-only, so none of that may appear in its output.
    result = _surface().call_tool(
        "get_surface_graph", {"op": "getApiFixturesValidation"}
    )
    blob = json.dumps(result)
    assert "Authorization" not in blob
    assert "X-Api-Token" not in blob
    assert "Bearer" not in blob


class _BareClient:
    """A duck-typed client with no ``surface_graph`` / ``plan_for`` (the catalog
    aggregator / a test fake). ``get_surface_graph`` must degrade gracefully, not raise."""

    surface_id = "bare"
    honeypots = False

    def list_tools(self) -> list[dict[str, Any]]:
        return []


def test_graceful_on_client_without_graph() -> None:
    surface = McpSurface(_BareClient())  # type: ignore[arg-type]
    scoped = surface.call_tool("get_surface_graph", {"op": "anything"})
    assert scoped["edges"] == []
    assert scoped["summary"] == {"operations": 0, "edges": 0}

    summary = surface.call_tool("get_surface_graph", {})
    assert summary["operations"] == []
    assert isinstance(summary["edges"], str)
