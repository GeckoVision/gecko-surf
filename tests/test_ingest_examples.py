"""Canonical example extraction (ingest enrichment, Part A1).

The spec's own examples are lifted onto ``Param.example`` and ``Operation.
field_examples`` — surface metadata (never a response payload). Two payoffs:
a real path/query example lets verify-docs --live build an honest call, and a
declared example is value-domain / correlation evidence. Absent examples must
yield empty, never crash.
"""

from __future__ import annotations

from pathlib import Path

from gecko.ingest import extract_operations, load_spec
from gecko.tools import to_tool

_FIX = Path(__file__).parent / "fixtures"


def _ops():
    spec = load_spec(str(_FIX / "examples_openapi.json"))
    return {op.operation_id: op for op in extract_operations(spec)}


def test_param_level_example_is_lifted() -> None:
    op = _ops()["get_token"]
    mint = next(p for p in op.parameters if p.name == "mint")
    assert mint.example == "So11111111111111111111111111111111111111112"


def test_schema_level_param_example_is_lifted() -> None:
    op = _ops()["get_token"]
    cluster = next(p for p in op.parameters if p.name == "cluster")
    # the example lives on the param's SCHEMA, not the param object — still lifted.
    assert cluster.example == "mainnet-beta"


def test_schema_property_examples_are_lifted_to_operation() -> None:
    op = _ops()["get_token"]
    assert (
        op.field_examples.get("mint") == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    )
    assert op.field_examples.get("decimals") == 6
    # a property with no example is simply absent (never a null placeholder).
    assert "symbol" not in op.field_examples


def test_absent_examples_yield_empty_no_crash() -> None:
    op = _ops()["health"]
    assert op.field_examples == {}
    # no parameters, and nothing carries an example.
    assert all(p.example is None for p in op.parameters)


def test_pegana_p0_has_no_spurious_examples() -> None:
    # the real design-partner spec ships no param examples -> empty, no crash.
    spec = load_spec(str(_FIX / "pegana_p0_openapi.json"))
    ops = extract_operations(spec)
    mint_params = [p for op in ops for p in op.parameters if p.name == "mint"]
    assert mint_params  # the spec really has mint path params
    assert all(p.example is None for p in mint_params)


def test_canonical_example_reaches_the_tool_schema() -> None:
    # the Tier-1 verify fix: the canonical example lands on the agent-facing schema so
    # example synthesis uses the SPEC's real value, not a placeholder that false-404s.
    op = _ops()["get_token"]
    tool = to_tool(op)
    props = tool["inputSchema"]["properties"]
    assert props["mint"].get("example") == (
        "So11111111111111111111111111111111111111112"
    )
