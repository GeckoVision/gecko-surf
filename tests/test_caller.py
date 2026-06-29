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
