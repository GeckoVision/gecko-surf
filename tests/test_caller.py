from pathlib import Path

import pytest

from gecko.caller import CallError, build_request
from gecko.ingest import extract_operations, load_spec
from gecko.tools import to_tool

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _tool(path: str, method: str = "GET"):
    op = next(
        o
        for o in extract_operations(load_spec(str(FIXTURE)))
        if o.path == path and o.method == method
    )
    return to_tool(op)


def test_path_and_query_placed_correctly():
    tool = _tool("/api/odds/snapshot/{fixtureId}")
    req = build_request(
        tool,
        {"fixtureId": 4242, "asOf": 999},
        base_url="https://txline.txodds.com",
    )
    assert req.method == "GET"
    assert "/api/odds/snapshot/4242" in req.url
    assert "asOf=999" in req.url


def test_auth_is_injected_not_supplied_by_agent():
    tool = _tool("/api/odds/snapshot/{fixtureId}")
    req = build_request(
        tool,
        {"fixtureId": 1},
        base_url="https://txline.txodds.com",
        auth={"Authorization": "Bearer TOK", "X-Api-Token": "APITOK"},
    )
    assert req.headers["Authorization"] == "Bearer TOK"
    assert req.headers["X-Api-Token"] == "APITOK"


def test_missing_path_param_is_caught():
    tool = _tool("/api/odds/snapshot/{fixtureId}")
    with pytest.raises(CallError):
        build_request(tool, {}, base_url="https://txline.txodds.com")


# FIX 2 — caller validates declared required fields (top-level + one level into body),
# not just path params, so a malformed call is caught instead of fired.
def _charge_tool() -> dict:
    return {
        "name": "create_charge",
        "description": "Create a charge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string"},
                "body": {
                    "type": "object",
                    "properties": {
                        "correlationID": {"type": "string"},
                        "value": {"type": "integer"},
                    },
                    "required": ["correlationID", "value"],
                },
            },
            "required": ["account", "body"],
        },
        "_invoke": {
            "method": "POST",
            "path": "/api/v1/charge",
            "param_locations": {"account": "query"},
        },
    }


def test_missing_required_top_level_and_body_fields_caught():
    with pytest.raises(CallError) as ei:
        build_request(_charge_tool(), {"body": {}}, base_url="https://api.example.test")
    msg = str(ei.value)
    assert "account" in msg
    assert "correlationID" in msg
    assert "value" in msg


def test_required_fields_present_builds_fine():
    req = build_request(
        _charge_tool(),
        {"account": "acct_1", "body": {"correlationID": "c1", "value": 100}},
        base_url="https://api.example.test",
    )
    assert req.method == "POST"
    assert "account=acct_1" in req.url
    assert req.json_body == {"correlationID": "c1", "value": 100}


def test_default_user_agent_set_to_avoid_waf_403():
    # urllib's default "Python-urllib" UA gets 403'd by Cloudflare WAFs (e.g. Colosseum).
    # build_request must default a self-identifying User-Agent when the caller gives none.
    tool = _tool("/api/odds/snapshot/{fixtureId}")
    req = build_request(tool, {"fixtureId": 1}, base_url="https://txline.txodds.com")
    ua = req.headers.get("User-Agent", "")
    assert ua and "python-urllib" not in ua.lower() and "gecko" in ua.lower()


def test_caller_provided_user_agent_wins_over_default():
    tool = _tool("/api/odds/snapshot/{fixtureId}")
    req = build_request(
        tool,
        {"fixtureId": 1},
        base_url="https://txline.txodds.com",
        auth={"User-Agent": "custom/1.0"},
    )
    assert req.headers["User-Agent"] == "custom/1.0"
