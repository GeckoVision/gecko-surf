"""Retrieval returns a SCOPE, not a surface.

The bug this pins: ``surface_all`` is an ENUMERATION rule (``list_tools`` shows every
usable tool below scale, so Gecko is never worse than the raw OpenAPI dump). It had
leaked into RETRIEVAL — ``search_capabilities`` called ``search_ranked``, which below
scale returns every usable tool, and then enriched every one of them with its full
``inputSchema``. So each search re-emitted the whole surface the agent already received
at connect: measured on the 43-op Pegana P0 fixture, five searches cost 91,089 B against
a 17,766 B connect — 5.1x the connect cost, entirely duplicate.

The split this suite enforces:

* ``list_tools`` owns BREADTH — every usable capability stays visible (full defs below
  scale, lightweight refs above). Recall is guaranteed HERE, and is untouched.
* ``search_capabilities`` owns DEPTH + ORDER — the scope for one intent: the ordered
  plan, and full schemas for exactly the ops that plan names. With no plan it falls
  back to the ranked top-k, capped. Never the whole surface, at any scale.

``AgentApiClient.search`` / ``search_ranked`` are deliberately NOT touched: they are the
library-level substrate the FCC eval arm (``fcc_eval.gecko_tools``) and the retrieval
benchmark measure, and the below-scale surface-all rule there is a measured FCC
guarantee (GECKO 1.00 -> 0.70 when it is disabled). This is an MCP-projection change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko.access import Session, public_session
from gecko.client import AgentApiClient
from gecko.evaluate import load_golden
from gecko.mcp_server import _REF_HINT, McpSurface
from gecko.scope import RETRIEVAL_MAX_TOOLS

FIX = Path(__file__).resolve().parent / "fixtures"
GOLDEN = FIX / "golden"


def _nbytes(obj: Any) -> int:
    """What the agent actually pays: the JSON both MCP transports put on the wire."""
    return len(json.dumps(obj, default=str, separators=(",", ":")).encode("utf-8"))


def _pegana() -> AgentApiClient:
    return AgentApiClient(str(FIX / "pegana_p0_openapi.json"))


def _txline() -> AgentApiClient:
    return AgentApiClient(str(FIX / "txline_openapi.yaml"))


def _privy() -> AgentApiClient:
    """The 159-op control surface. A session WITH auth, so every op stays usable and the
    surface is genuinely above scale (a public session hides the auth-gated ops and drops
    it back under the threshold — which would silently test the wrong branch)."""
    return AgentApiClient(
        str(GOLDEN / "privy_openapi.json"),
        session=Session(jwt="recorded-mode", api_token="recorded-mode"),
    )


def _txodds() -> AgentApiClient:
    return AgentApiClient(
        str(FIX / "txodds_docs.yaml"),
        session=Session(jwt="recorded-mode", api_token="recorded-mode"),
    )


def _search(surface: McpSurface, query: str) -> dict[str, Any]:
    return surface.call_tool("search_capabilities", {"query": query})


# --- (A) the envelope: a plan plus exactly the tools the plan names ---------------


def test_search_returns_plan_and_tools_envelope() -> None:
    """The frozen new contract: a mapping with ``plan`` (or None) and ``tools``."""
    result = _search(McpSurface(_pegana()), "consume magic link")
    assert isinstance(result, dict)
    assert set(result) == {"plan", "tools"}
    assert isinstance(result["tools"], list)


def test_plan_scope_is_exactly_the_ops_the_plan_names_in_order() -> None:
    """The scope IS the plan: every step's op, in derivation order, goal last — the
    ordering and the join a flat ranked list cannot carry."""
    result = _search(McpSurface(_pegana()), "consume magic link")
    plan = result["plan"]
    assert plan is not None
    ordered = [s["operation_id"] for s in plan["steps"]]
    assert ordered == ["me", "mint_magic", "consume_magic"]
    assert [t["name"] for t in result["tools"]] == ordered


def test_every_tool_in_scope_carries_its_full_input_schema() -> None:
    """Strictly MORE informative: each scoped op ships its real callable schema, so the
    agent can execute the whole chain without a second round trip."""
    result = _search(McpSurface(_pegana()), "consume magic link")
    for tool in result["tools"]:
        assert set(tool) >= {"name", "summary", "path", "method", "inputSchema"}
        assert tool["inputSchema"]["type"] == "object"
    # Control plane only: retrieval never hands out the wire-routing block.
    assert all("_invoke" not in t for t in result["tools"])


def test_no_plan_falls_back_to_capped_ranked_hits_not_the_surface() -> None:
    """No chain needed -> ranked hits, CAPPED. Never the whole surface."""
    client = _pegana()
    assert client.surface_all, "fixture must be below scale for this to be the bug"
    result = _search(McpSurface(client), "list assets")
    assert result["plan"] is None
    assert result["tools"], "a genuine intent must still return candidates"
    assert len(result["tools"]) <= RETRIEVAL_MAX_TOOLS
    assert result["tools"][0]["name"] == "list_assets"


# --- (D) the cause of the duplication: surface_all must not reach retrieval -------


@pytest.mark.parametrize("factory", [_pegana, _txline, _txodds])
def test_retrieval_is_capped_even_below_scale(factory) -> None:
    """The regression that mattered: a below-scale surface must not dump every usable
    tool (with full schemas) on every single search."""
    client = factory()
    assert client.surface_all
    surface = McpSurface(client)
    usable = len(client.list_tools())
    for query in ("anything at all", "list", "get the thing"):
        tools = _search(surface, query)["tools"]
        assert len(tools) <= max(RETRIEVAL_MAX_TOOLS, 0) + 8, (
            "a plan may widen the scope past the flat cap, but never to the surface"
        )
        assert len(tools) < usable


def test_a_search_costs_far_less_than_the_connect_surface() -> None:
    """The measured claim: a search is a fraction of the connect cost, not a multiple."""
    client = _pegana()
    surface = McpSurface(client)
    connect = _nbytes(surface.list_tools())
    searches = [
        "consume magic link",
        "patch a webhook",
        "delete a webhook",
        "list assets",
        "get peg state by mint",
    ]
    total = sum(_nbytes(_search(surface, q)) for q in searches)
    assert total < connect, (
        f"five searches ({total} B) must cost less than one connect ({connect} B); "
        "they used to cost 5.1x it"
    )


def test_client_search_substrate_is_untouched() -> None:
    """Guard the lane boundary: the FCC eval arm and the retrieval benchmark read
    ``client.search``/``search_ranked``, where the below-scale surface-all rule is a
    measured FCC guarantee. This PR changes the MCP PROJECTION, not that substrate."""
    client = _pegana()
    usable = {t["name"] for t in client.list_tools()}
    assert {h["name"] for h in client.search("anything at all", limit=5)} == usable


# --- breadth stays where it belongs: recall is guaranteed by enumeration ----------


@pytest.mark.parametrize(
    "name,factory",
    [
        (
            "pegana",
            lambda: AgentApiClient(
                str(FIX / "pegana_openapi.json"), session=public_session()
            ),
        ),
        ("txodds", _txodds),
    ],
)
def test_every_golden_expected_op_stays_visible_in_list_tools(name, factory) -> None:
    """Falsifier 2's real guard on the MCP path: capping RETRIEVAL cannot lose an op,
    because BREADTH is served by ``list_tools`` — every golden task's expected op is
    enumerated there, at full definition below scale."""
    surface = McpSurface(factory())
    listed = {t["name"] for t in surface.list_tools()}
    for task in load_golden(GOLDEN / f"{name}_tasks.jsonl"):
        if not task.expect_ops:
            continue
        assert set(task.expect_ops) & listed, (
            f"{name}/{task.archetype}: {task.expect_ops} not enumerated for {task.goal!r}"
        )


# --- (C) the cheap door has to be findable ---------------------------------------


def test_get_capability_is_advertised_in_list_tools() -> None:
    """An agent that has not read our docs cannot find a tool that is not listed —
    ``get_capability`` was callable-by-name only, so nobody called it."""
    tools = McpSurface(_privy()).list_tools()
    by_name = {t["name"]: t for t in tools}
    assert "get_capability" in by_name
    schema = by_name["get_capability"]["inputSchema"]
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["required"] == ["name"]
    # search_capabilities stays the first tool an agent sees.
    assert tools[0]["name"] == "search_capabilities"


def test_ref_hint_points_at_the_cheap_door() -> None:
    """Above scale every ref used to route the agent through ``search_capabilities`` —
    the expensive door — to recover one schema it already knew the name of."""
    assert "get_capability" in _REF_HINT
    assert "search_capabilities" not in _REF_HINT
    refs = [
        t
        for t in McpSurface(_privy()).list_tools()
        if t["name"] not in {"search_capabilities", "query_docs", "get_capability"}
    ]
    assert refs and all(_REF_HINT in t["description"] for t in refs)


def test_get_capability_is_cheaper_than_search_for_a_known_name() -> None:
    """The measured 4x: fetching three schemas you already named must not cost three
    ranked searches."""
    client = _privy()
    surface = McpSurface(client)
    names = [t["name"] for t in client.list_tools()[:3]]
    direct = sum(
        _nbytes(surface.call_tool("get_capability", {"name": n})) for n in names
    )
    via_search = sum(_nbytes(_search(surface, n)) for n in names)
    assert direct * 2 < via_search, (
        f"get_capability {direct} B vs search {via_search} B"
    )


# --- falsifier 3: privy (above scale) is the control -----------------------------


def test_privy_above_scale_retrieval_does_not_regress() -> None:
    """Privy is already ``surface_all=False``; retrieval there was already ranked and
    capped, so the scope change must not make it bigger or lose its top hit."""
    client = _privy()
    assert client.surface_all is False
    surface = McpSurface(client)
    for query in ("create a wallet", "authenticate a user"):
        ranked = [h.name for h in client.search_ranked(query)]
        result = _search(surface, query)
        assert result["tools"], query
        assert result["tools"][0]["name"] == ranked[0]
        assert len(result["tools"]) <= len(ranked)
