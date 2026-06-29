from pathlib import Path

from gecko.ingest import extract_operations, load_spec
from gecko.tools import build_tools, to_tool

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _ops():
    return extract_operations(load_spec(str(FIXTURE)))


def _odds_snapshot():
    return next(
        o
        for o in _ops()
        if o.path == "/api/odds/snapshot/{fixtureId}" and o.method == "GET"
    )


def test_builds_a_tool_per_operation():
    assert len(build_tools(_ops())) == 18


def test_tool_exposes_meaningful_inputs():
    t = to_tool(_odds_snapshot())
    props = t["inputSchema"]["properties"]
    assert "fixtureId" in props
    assert "fixtureId" in t["inputSchema"]["required"]
    assert "asOf" in props
    assert "asOf" not in t["inputSchema"].get("required", [])


def test_auth_headers_are_hidden_from_agent():
    props = to_tool(_odds_snapshot())["inputSchema"]["properties"]
    assert "Authorization" not in props
    assert "X-Api-Token" not in props


def test_description_is_question_shaped():
    d = to_tool(_odds_snapshot())["description"].lower()
    assert "odds" in d
    assert "fixtureid" in d  # required input surfaced in prose


def test_invoke_metadata_present():
    inv = to_tool(_odds_snapshot())["_invoke"]
    assert inv["method"] == "GET"
    assert inv["path"] == "/api/odds/snapshot/{fixtureId}"
    assert inv["param_locations"]["fixtureId"] == "path"
