"""Don't ask the agent for a value the SPEC already fixed.

A JSON-RPC-over-HTTP API (Jito's Block Engine is the live case) requires an envelope:
``jsonrpc: "2.0"``, an ``id``, and ``method: "<the operation this tool already IS>"``.
Those are not decisions — there is exactly one legal answer — so putting them in the
agent-facing schema is asking the agent to restate boilerplate, and MCP's own input
validation then rejects the call when it doesn't.

The rule is generic (it reads JSON Schema, never a provider) and narrow: only REQUIRED
fields, so no call that works today changes shape.
"""

from __future__ import annotations

from typing import Any

from gecko.caller import apply_spec_fixed, build_request
from gecko.tools import build_tools, strip_spec_fixed_required
from gecko.ingest import extract_operations

RPC_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "rpc", "version": "1"},
    "servers": [{"url": "https://rpc.test"}],
    "paths": {
        "/api/v1/getThings": {
            "post": {
                "operationId": "getThings",
                "summary": "get the things",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["jsonrpc", "id", "method", "params"],
                                "properties": {
                                    "jsonrpc": {"type": "string", "const": "2.0"},
                                    "id": {"type": "integer", "default": 1},
                                    "method": {
                                        "type": "string",
                                        "const": "getThings",
                                    },
                                    "params": {"type": "array", "default": []},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/api/v1/sendThing": {
            "post": {
                "operationId": "sendThing",
                "summary": "send a thing",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["jsonrpc", "method", "params"],
                                "properties": {
                                    "jsonrpc": {"type": "string", "const": "2.0"},
                                    "method": {"type": "string", "const": "sendThing"},
                                    "params": {"type": "array"},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _tools() -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in build_tools(extract_operations(RPC_SPEC))}


# --- the agent-facing schema stops asking --------------------------------------


def test_an_op_with_a_fully_fixed_envelope_asks_the_agent_for_nothing() -> None:
    tool = _tools()["getThings"]

    assert tool["inputSchema"].get("required") in (None, [])
    # The fields stay visible (an agent may still override) — they are just not demanded.
    assert "jsonrpc" in tool["inputSchema"]["properties"]["body"]["properties"]


def test_the_agent_is_still_asked_for_the_part_it_must_decide() -> None:
    tool = _tools()["sendThing"]

    assert tool["inputSchema"]["required"] == ["body"]
    assert tool["inputSchema"]["properties"]["body"]["required"] == ["params"]


def test_the_fixed_values_are_carried_for_the_caller() -> None:
    fixed = _tools()["getThings"]["_invoke"]["spec_fixed"]["body"]

    assert fixed == {"jsonrpc": "2.0", "id": 1, "method": "getThings", "params": []}


# --- the caller supplies them --------------------------------------------------


def test_a_zero_arg_call_still_builds_the_full_envelope() -> None:
    tool = _tools()["getThings"]

    req = build_request(tool, {}, "https://rpc.test")

    assert req.url == "https://rpc.test/api/v1/getThings"
    assert req.json_body == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getThings",
        "params": [],
    }


def test_the_agents_own_value_is_never_overwritten() -> None:
    tool = _tools()["getThings"]

    req = build_request(tool, {"body": {"id": 99, "params": ["x"]}}, "https://rpc.test")

    assert req.json_body["id"] == 99
    assert req.json_body["params"] == ["x"]
    assert req.json_body["method"] == "getThings"  # still supplied


def test_a_partially_fixed_op_merges_agent_args_with_the_envelope() -> None:
    tool = _tools()["sendThing"]

    req = build_request(tool, {"body": {"params": ["tx"]}}, "https://rpc.test")

    assert req.json_body == {"jsonrpc": "2.0", "method": "sendThing", "params": ["tx"]}


# --- narrow by construction ----------------------------------------------------


def test_an_optional_field_with_a_default_is_left_alone() -> None:
    # Only REQUIRED fields are filled: an optional default must NOT start riding along,
    # or calls that work today would silently change shape.
    schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "default": 10}},
    }

    out, fixed = strip_spec_fixed_required(schema)

    assert fixed == {}
    assert out is schema  # byte-identical: nothing to strip


def test_a_spec_that_fixes_nothing_carries_no_marker() -> None:
    plain = {
        "openapi": "3.0.3",
        "info": {"title": "p", "version": "1"},
        "paths": {
            "/x/{id}": {
                "get": {
                    "operationId": "getX",
                    "summary": "get x",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    tool = build_tools(extract_operations(plain))[0]

    assert "spec_fixed" not in tool["_invoke"]
    assert tool["inputSchema"]["required"] == ["id"]


def test_apply_spec_fixed_is_a_no_op_without_the_marker() -> None:
    args = {"a": 1}

    assert apply_spec_fixed({"method": "GET", "path": "/x"}, args) is args
