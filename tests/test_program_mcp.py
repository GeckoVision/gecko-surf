"""Phase 3: the program-graph MCP surface — offline (Pattern B).

The falsifiable core: build the surface from the Anchor IDL + ORE source and drive
its tools directly, with no server. derive_pda returns the real mainnet address; the
surface is duck-typed so the existing transports serve it unchanged.
"""

from __future__ import annotations

from gecko.http_server import _surface_from
from gecko.program_mcp import ProgramGraphSurface, build_program_surface
from tests.test_pda_extract import ORE_PROGRAM, ORE_SOURCE
from tests.test_pda_idl import ANCHOR_IDL

ORE_CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"


def _surface() -> ProgramGraphSurface:
    return build_program_surface(idl=ANCHOR_IDL, source=ORE_SOURCE)


def test_lists_three_question_shaped_tools() -> None:
    tools = _surface().list_tools()
    assert {t["name"] for t in tools} == {
        "get_program_graph",
        "plan_instruction",
        "derive_pda",
    }
    for t in tools:  # first-call-correct: every tool ships a full input schema
        assert t["inputSchema"]["type"] == "object"


def test_get_program_graph_returns_structured_json() -> None:
    graph = _surface().call_tool("get_program_graph", {})
    assert graph["program_id"] == ORE_PROGRAM
    assert "config" in graph["pdas"]
    assert any(i["name"] == "open_round" for i in graph["instructions"])


def test_derive_pda_returns_mainnet_address() -> None:
    """The actionable tool: an agent asks to derive 'config' and gets the real
    deployed address — no friction, no hand-derivation."""
    out = _surface().call_tool("derive_pda", {"account": "config"})
    assert out["address"] == ORE_CONFIG
    assert 0 <= out["bump"] <= 255


def test_derive_pda_dynamic_with_bindings() -> None:
    out = _surface().call_tool(
        "derive_pda", {"account": "miner", "bindings": {"authority": ORE_CONFIG}}
    )
    assert len(out["address"]) >= 32
    assert "error" not in out


def test_derive_pda_missing_binding_explains_what_is_needed() -> None:
    out = _surface().call_tool("derive_pda", {"account": "miner"})  # no authority
    assert "error" in out
    assert "authority" in out["needs"]  # required_bindings names to supply


def test_derive_pda_unresolvable_is_flagged_not_guessed() -> None:
    out = _surface().call_tool("derive_pda", {"account": "vault"})
    assert out["resolvable"] is False
    assert out["unresolved"]
    assert "address" not in out  # never fabricates one


def test_derive_pda_unknown_account_lists_available() -> None:
    out = _surface().call_tool("derive_pda", {"account": "nope"})
    assert "error" in out
    assert "config" in out["available"]


def test_plan_instruction_returns_ordered_plan() -> None:
    plan = _surface().call_tool("plan_instruction", {"instruction": "open_round"})
    assert set(plan["derivation_order"]) == {"round", "miner"}
    planned = {s["account"] for s in plan["plan"]}
    assert planned == {"round", "miner"}


def test_plan_marks_a_cyclic_step_unorderable() -> None:
    """The agent-facing plan must not present an arbitrary position as a real one."""
    from gecko.program_graph import build_program_graph

    idl = {
        "address": ORE_PROGRAM,
        "instructions": [
            {
                "name": "tangle",
                "args": [],
                "accounts": [
                    {"name": "a", "pda": {"seeds": [{"kind": "account", "path": "b"}]}},
                    {"name": "b", "pda": {"seeds": [{"kind": "account", "path": "a"}]}},
                ],
            }
        ],
    }
    surface = ProgramGraphSurface(build_program_graph(idl=idl))
    plan = surface.call_tool("plan_instruction", {"instruction": "tangle"})
    assert plan["cycle"] == ["a", "b"]
    assert all(s["unorderable"] is True for s in plan["plan"])
    assert all(s["resolvable"] is False for s in plan["plan"])


def test_plan_of_an_acyclic_instruction_is_orderable() -> None:
    plan = _surface().call_tool("plan_instruction", {"instruction": "open_round"})
    assert plan["cycle"] == []
    assert all(s["unorderable"] is False for s in plan["plan"])


def test_plan_unknown_instruction_lists_available() -> None:
    out = _surface().call_tool("plan_instruction", {"instruction": "nope"})
    assert "error" in out
    assert "open_round" in out["available"]


def test_unknown_tool_is_reported() -> None:
    out = _surface().call_tool("bogus", {})
    assert "error" in out


def test_surface_is_servable_by_existing_transports() -> None:
    """Duck-typed (list_tools + call_tool) => the existing HTTP/stdio serve path
    accepts it unchanged, no server needed to prove it."""
    surface = _surface()
    resolved = _surface_from(surface, None, "recorded")
    assert resolved is surface
