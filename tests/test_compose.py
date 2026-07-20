"""Step-5 compose tests (§12 Phase 4): per-surface graphs composed at plan time
(never merged), cross-surface plans DECLARED-only (the §13.6 lock), and the
success metric itself — a REAL two-API intent ("get the fixture from TxLINE,
open a market on it") first-plan-correct offline through evaluate_cross_chain.

TxLINE is the real bundled spec; the market API is a realistic fixture spec with
provider-authored x-gecko hints. TxLINE's side of the entity vocabulary comes in
as a customer confirmation (declared_hints) — both DECLARED sources exercised.
Offline, recorded mode, $0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gecko.chain_eval import evaluate_cross_chain
from gecko.client import AgentApiClient
from gecko.compose import ComposeError, Workspace, cross_plan
from gecko.graph import build_graph
from gecko.ingest import extract_operations, load_spec

FIX = Path(__file__).resolve().parent / "fixtures"
TXLINE = FIX / "txline_openapi.yaml"

#: TxLINE-side DECLARED vocabulary — the customer-confirmed path (§12): what
#: `gecko graph confirm txline FixtureId fixture` would persist.
TXLINE_HINTS = {"FixtureId": "fixture", "fixtureId": "fixture"}


def market_spec(*, with_hints: bool = True) -> dict[str, Any]:
    """A small on-chain-markets API: open a market on a fixture. The consuming
    param is named ``fixture_ref`` — a SYNONYM of TxLINE's ``FixtureId``, so no
    name-match can ever join them; only the DECLARED entity can (§13.6)."""
    param: dict[str, Any] = {
        "name": "fixture_ref",
        "in": "query",
        "required": True,
        "schema": {"type": "integer", "format": "int64"},
    }
    if with_hints:
        param["x-gecko-entity"] = "fixture"
    return {
        "openapi": "3.0.0",
        "info": {"title": "Gorilla Markets", "version": "1"},
        "servers": [{"url": "https://markets.example.com"}],
        "paths": {
            "/markets/open": {
                "get": {
                    "operationId": "openMarket",
                    "summary": "Open a prediction market on a fixture",
                    "parameters": [
                        param,
                        {
                            "name": "side",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "marketId": {"type": "string"},
                                            "state": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }


def _txline_client() -> AgentApiClient:
    return AgentApiClient(str(TXLINE), surface_id="txline", declared_hints=TXLINE_HINTS)


def _market_client(**kw) -> AgentApiClient:
    return AgentApiClient(market_spec(**kw), surface_id="market")


def _workspace(tx: AgentApiClient, mk: AgentApiClient) -> Workspace:
    return Workspace(graphs=(tx.surface_graph, mk.surface_graph))


# --- workspace validation --------------------------------------------------------
def test_workspace_rejects_unnamespaced_and_duplicate_graphs() -> None:
    ops = extract_operations(load_spec(str(TXLINE)))
    with pytest.raises(ComposeError):
        Workspace(graphs=(build_graph(ops),))  # empty surface_id
    g = build_graph(ops, surface_id="x")
    with pytest.raises(ComposeError):
        Workspace(graphs=(g, g))  # duplicate


# --- the cross plan --------------------------------------------------------------
def test_cross_plan_two_api_intent() -> None:
    """The §12 Phase 4 shape: TxLINE supplies the fixture id, the market surface
    consumes it via a DECLARED synonym join."""
    tx, mk = _txline_client(), _market_client()
    p = cross_plan(_workspace(tx, mk), "market", "openMarket", set())
    assert p is not None
    assert [s.surface for s in p.steps] == ["txline", "market"]
    assert p.steps[-1].operation_id == "openMarket"
    assert "fixture_ref" in p.steps[0].supplies
    cross = [e for e in p.explain if e.param == "fixture_ref"]
    assert cross and cross[0].provenance == "DECLARED"
    assert cross[0].basis == "declared:fixture"
    assert cross[0].source_surface == "txline"
    assert cross[0].source_field == "FixtureId"


def test_cross_plan_is_declared_only() -> None:
    """No hints on the market side -> no cross basis -> honest None. The name
    ``fixture_ref`` still 'looks' joinable; the §13.6 lock says looks don't count."""
    tx = _txline_client()
    mk = _market_client(with_hints=False)
    assert cross_plan(_workspace(tx, mk), "market", "openMarket", set()) is None


def test_cross_plan_needs_both_sides_declared() -> None:
    """Market declares, but TxLINE has no confirmed vocabulary -> None: a
    one-sided declaration is not an entity identity."""
    tx = AgentApiClient(str(TXLINE), surface_id="txline")  # no declared_hints
    mk = _market_client()
    assert cross_plan(_workspace(tx, mk), "market", "openMarket", set()) is None


def test_intra_plan_untouched_when_chain_closes_at_home() -> None:
    """A TxLINE-internal intent never grows cross steps: intra-first."""
    tx, mk = _txline_client(), _market_client()
    p = cross_plan(_workspace(tx, mk), "txline", "getApiOddsUpdatesFixtureid", set())
    assert p is not None
    assert all(s.surface == "txline" for s in p.steps)


def test_satisfied_intent_yields_trivial_intent_plan() -> None:
    """When the agent already holds the fixture ref, the cross machinery must not
    invent a supplier: the plan is the intent op alone (planner-layer suppression
    of single-step plans still applies downstream)."""
    tx, mk = _txline_client(), _market_client()
    p = cross_plan(_workspace(tx, mk), "market", "openMarket", {"fixture_ref"})
    assert p is not None and len(p.steps) == 1


# --- the success metric: cross-API chain-FCC, offline ---------------------------
def test_two_api_chain_first_plan_correct_offline() -> None:
    """THE Step-5 gate (§12): the composed two-API plan executes end-to-end in
    recorded mode — TxLINE's synthesized FixtureId threads into the market op's
    fixture_ref and lands kind-correct. First-plan-correct, offline, $0."""
    tx, mk = _txline_client(), _market_client()
    p = cross_plan(_workspace(tx, mk), "market", "openMarket", set())
    assert p is not None
    result = evaluate_cross_chain({"txline": tx, "market": mk}, p)
    assert result.all_well_formed, result.reason
    assert result.first_plan_correct, result.reason
    thread = next(t for t in result.threads if t.param == "fixture_ref")
    assert thread.found and thread.kind_ok
    assert thread.source_op != "openMarket"  # produced on the other surface


def test_cross_chain_missing_client_is_honest_failure() -> None:
    tx, mk = _txline_client(), _market_client()
    p = cross_plan(_workspace(tx, mk), "market", "openMarket", set())
    assert p is not None
    result = evaluate_cross_chain({"market": mk}, p)  # txline client absent
    assert not result.first_plan_correct
    assert "no client for surface 'txline'" in result.reason
