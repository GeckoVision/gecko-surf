"""The Surface façade — one named handle over the shipped engine.

These assert *delegation and legibility*, not new behavior: the Surface must expose the
same call graph, tools, safety stance, and projections the engine already produces, through
one noun. Behavior is covered by the engine's own suites; here we prove the façade wires
them together and adds nothing that could drift from them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.surface import SafetyVerdict, Surface

FIX = Path(__file__).resolve().parent / "fixtures"
TXLINE = str(FIX / "txline_openapi.yaml")


def _surface() -> Surface:
    return Surface.from_spec(TXLINE, session=public_session())


# --- construction + identity ------------------------------------------------------


def test_from_spec_builds_a_surface_over_a_client() -> None:
    s = _surface()
    assert isinstance(s.client, AgentApiClient)


def test_of_wraps_an_existing_client() -> None:
    client = AgentApiClient(TXLINE, session=public_session())
    s = Surface.of(client)
    assert s.client is client


def test_surface_id_delegates() -> None:
    client = AgentApiClient(TXLINE, session=public_session(), surface_id="txline")
    assert Surface.of(client).surface_id == "txline"


# --- the call graph is the same object the engine produces ------------------------


def test_graph_is_the_clients_surface_graph() -> None:
    s = _surface()
    assert s.graph is s.client.surface_graph  # same object, not a copy
    assert len(s.graph.nodes) > 0 and len(s.graph.edges) > 0


def test_plan_delegates_to_the_engine_plan() -> None:
    """The Surface's plan for an intent equals the engine's plan for the same top tool —
    the façade sources the top tool, then delegates verbatim."""
    s = _surface()
    intent = "get live odds updates for a fixture"
    hits = s.client.search(intent, limit=1)
    if not hits:
        pytest.skip("fixture produced no search hit for the intent")
    top = hits[0]["name"]
    assert s.plan(intent) == s.client.plan_for(intent, top)


def test_plan_with_an_explicit_tool_targets_that_tool() -> None:
    s = _surface()
    tools = s.tools()
    if not tools:
        pytest.skip("fixture produced no tools")
    name = tools[0]["name"]
    assert s.plan("anything", tool=name) == s.client.plan_for("anything", name)


# --- tools + search delegate ------------------------------------------------------


def test_tools_delegates_to_list_tools() -> None:
    s = _surface()
    assert s.tools() == s.client.list_tools()


def test_search_delegates() -> None:
    s = _surface()
    assert s.search("odds", limit=3) == s.client.search("odds", limit=3)


# --- the safety verdict reflects the per-tool quarantine --------------------------


def test_safety_is_clean_on_a_trusted_spec() -> None:
    s = _surface()
    verdict = s.safety
    assert isinstance(verdict, SafetyVerdict)
    assert verdict.clean is True
    assert verdict.quarantined == ()
    assert verdict.total_tools == len(s.tools())


def test_safety_reports_quarantined_tools() -> None:
    """A quarantined tool must surface in the verdict — this is what a compose partner
    (a policy/approval gate) reads. Simulate by poisoning one tool name."""
    s = _surface()
    tools = s.tools()
    if not tools:
        pytest.skip("fixture produced no tools")
    victim = tools[0]["name"]
    s.client._poisoned_tool_names.add(victim)
    verdict = s.safety
    assert victim in verdict.quarantined
    assert verdict.clean is False


def test_safety_all_quarantined_is_the_degenerate_case() -> None:
    v = SafetyVerdict(total_tools=3, quarantined=("a", "b", "c"))
    assert v.all_quarantined is True
    assert SafetyVerdict(total_tools=3, quarantined=("a",)).all_quarantined is False
    assert SafetyVerdict(total_tools=0, quarantined=()).all_quarantined is False


# --- projections: one artifact, N shapes -----------------------------------------


def test_projections_delegates_to_build_artifacts() -> None:
    from gecko.agentnative import build_artifacts

    s = _surface()
    assert s.projections() == build_artifacts(s.client)


def test_project_returns_one_named_projection() -> None:
    s = _surface()
    llms = s.project("llms.txt")
    assert isinstance(llms, str) and llms
    assert llms == s.projections()["llms.txt"]


def test_project_unknown_kind_raises() -> None:
    s = _surface()
    with pytest.raises(KeyError, match="unknown projection"):
        s.project("nope.txt")  # type: ignore[arg-type]
