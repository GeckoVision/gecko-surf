"""Planner wiring (§5, §12 Phase 1) — PROVE ``graph.plan()`` reaches an agent.

``graph.plan()`` was built and chain-FCC-proven but orphaned: imported nowhere but
tests, so chain comprehension was dark ("wired != reaches the agent"). These tests
prove the wiring end to end:

- a chain-shaped intent (needs an id/seq it can't supply) surfaces a ``plan`` block
  with the correct ordered steps + provenance-carrying ``explain``;
- a satisfiable intent surfaces NO plan (flat search untouched — the regression guard);
- and the plan travels all the way to a caller through ``search_capabilities`` over the
  REAL streamable-HTTP MCP transport (the "reaches the agent" proof — a catalog unit
  test does not satisfy this).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gecko.client import AgentApiClient
from gecko.ingest import extract_operations, load_spec
from gecko.mcp_server import McpSurface
from gecko.planner import plan_for_query, satisfiable_inputs

FIX = Path(__file__).resolve().parent / "fixtures"
TXLINE = FIX / "txline_openapi.yaml"

# The two known TxLINE chains (spec §7) — the seed the wiring must surface.
ODDS_INTENT = "get live odds updates"  # needs fixtureId it can't supply -> chain
ODDS_SATISFIABLE = "live odds updates for fixture 12345"  # names the fixture -> flat
STAT_INTENT = "validate a score stat merkle proof for a fixture with stat key 1"


def _client() -> AgentApiClient:
    return AgentApiClient(str(TXLINE))


def _ops():
    return extract_operations(load_spec(str(TXLINE)))


def _op(operation_id: str):
    return next(o for o in _ops() if o.operation_id == operation_id)


# --- satisfiability: the discriminator for "is a chain needed?" -----------------
def test_satisfiable_inputs_reads_intent_not_op_alone() -> None:
    odds = _op("getApiOddsUpdatesFixtureid")
    # bare intent supplies nothing the op needs...
    assert satisfiable_inputs(ODDS_INTENT, odds) == frozenset()
    # ...but naming the fixture satisfies the required fixtureId path param.
    assert satisfiable_inputs(ODDS_SATISFIABLE, odds) == frozenset({"fixtureId"})


def test_satisfiable_inputs_covers_non_id_flow_keys() -> None:
    stat = _op("getApiScoresStat-validation")
    sat = satisfiable_inputs(STAT_INTENT, stat)
    # fixture + stat key are named in the intent; seq is NOT -> seq is the missing key.
    assert "fixtureId" in sat and "statKey" in sat
    assert "seq" not in sat


# --- plan_for_query: chain when needed, None (flat) when satisfiable ------------
def test_chain_intent_yields_ordered_plan_with_provenance() -> None:
    g = _client().surface_graph
    plan = plan_for_query(g, _op("getApiOddsUpdatesFixtureid"), ODDS_INTENT)
    assert plan is not None
    assert [s["operation_id"] for s in plan["steps"]] == [
        "getApiFixturesSnapshot",
        "getApiOddsUpdatesFixtureid",
    ]
    # the supplier step supplies fixtureId to the intent step.
    assert "fixtureId" in plan["steps"][0]["supplies"]
    # provenance is preserved end-to-end: INFERRED, entity basis, high, real source field.
    (entry,) = plan["explain"]
    assert entry["param"] == "fixtureId"
    assert entry["source_field"] == "FixtureId"
    assert entry["provenance"] == "INFERRED"
    assert entry["basis"] == "entity:fixture"
    assert entry["confidence"] == "high"


def test_chain2_seq_is_sourced_with_rare_key_provenance() -> None:
    g = _client().surface_graph
    plan = plan_for_query(g, _op("getApiScoresStat-validation"), STAT_INTENT)
    assert plan is not None
    assert plan["steps"][-1]["operation_id"] == "getApiScoresStat-validation"
    seq = [e for e in plan["explain"] if e["param"] == "seq"]
    assert len(seq) == 1
    assert seq[0]["source_field"] == "seq"
    assert seq[0]["source_op"].startswith("getApiScores")
    assert seq[0]["basis"] == "rare-key:seq"
    assert seq[0]["provenance"] == "INFERRED" and seq[0]["confidence"] == "high"


def test_satisfiable_intent_yields_no_plan() -> None:
    """Regression guard: when required inputs ARE satisfiable, no plan is invented —
    a trivial one-step plan is suppressed so simple queries never grow a plan block."""
    g = _client().surface_graph
    assert (
        plan_for_query(g, _op("getApiOddsUpdatesFixtureid"), ODDS_SATISFIABLE) is None
    )
    # an op with no required inputs is likewise flat (no chain to build).
    assert plan_for_query(g, _op("getApiFixturesSnapshot"), "latest fixtures") is None


# --- McpSurface projection: the plan rides the top search hit -------------------
def test_search_capabilities_attaches_plan_to_top_hit() -> None:
    surface = McpSurface(_client())
    hits = surface.call_tool("search_capabilities", {"query": ODDS_INTENT})
    assert hits and hits[0]["name"] == "getApiOddsUpdatesFixtureid"
    plan = hits[0].get("plan")
    assert plan is not None
    assert [s["operation_id"] for s in plan["steps"]] == [
        "getApiFixturesSnapshot",
        "getApiOddsUpdatesFixtureid",
    ]
    assert plan["explain"][0]["basis"] == "entity:fixture"
    # only the top hit carries a plan; the rest are plain flat-search hits.
    assert all("plan" not in h for h in hits[1:])


def test_search_capabilities_no_plan_on_satisfiable_intent() -> None:
    surface = McpSurface(_client())
    hits = surface.call_tool("search_capabilities", {"query": ODDS_SATISFIABLE})
    assert hits
    assert all("plan" not in h for h in hits)  # flat search untouched


# --- the "reaches the agent" proof: real streamable-HTTP MCP transport ----------
def test_plan_reaches_agent_over_streamable_http_mcp() -> None:
    """Spin up the ACTUAL surface behind the real JSON-RPC streamable-HTTP transport,
    connect with the real mcp client, and assert the plan block travels all the way to
    a caller through ``search_capabilities``. This is the direct end-to-end probe the
    orphaned-graph gap demanded — not a catalog unit test."""
    import pytest

    pytest.importorskip("mcp")
    import anyio
    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from gecko.http_server import build_http_app

    base = "http://test"

    async def _call(name: str, args: dict[str, Any]) -> str:
        # A fresh app per connection (the streamable-HTTP session manager can only run
        # once per instance). A client with auth present (stub_session) so the auth-gated
        # TxLINE ops are usable and searchable; a bare-spec app would use a public session
        # and hide them.
        app = build_http_app(_client(), allowed_hosts=["test"], allowed_origins=[base])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=base
            ) as http_client:
                async with streamable_http_client(
                    f"{base}/mcp", http_client=http_client
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        res = await session.call_tool(name, args)
                        return res.content[0].text  # type: ignore[union-attr]

    raw = anyio.run(_call, "search_capabilities", {"query": ODDS_INTENT})
    hits = json.loads(raw)
    assert isinstance(hits, list) and hits
    top = hits[0]
    assert top["name"] == "getApiOddsUpdatesFixtureid"
    # THE PROOF: the plan block survived the wire, ordered + with provenance intact.
    plan = top["plan"]
    assert [s["operation_id"] for s in plan["steps"]] == [
        "getApiFixturesSnapshot",
        "getApiOddsUpdatesFixtureid",
    ]
    explain = plan["explain"][0]
    assert explain["param"] == "fixtureId"
    assert explain["source_field"] == "FixtureId"
    assert explain["provenance"] == "INFERRED"
    assert explain["basis"] == "entity:fixture"
    assert explain["confidence"] == "high"

    # And the regression guard survives the wire too: a satisfiable intent stays flat.
    raw_flat = anyio.run(_call, "search_capabilities", {"query": ODDS_SATISFIABLE})
    flat_hits = json.loads(raw_flat)
    assert flat_hits and all("plan" not in h for h in flat_hits)
