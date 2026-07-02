"""Priority 4 — tools-rev integrity + control-plane corpus capture.

``tools_rev`` re-derives the tool set from the pinned spec and re-asserts it before
serving, so an in-memory tamper is caught not shipped. ``client.call`` and
``validator.validate_all`` accrue a local correctness corpus through the SAME narrow
``corpus.outcome_from`` boundary the HTTP server uses — metadata only, never a value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.caller import CallError, build_request
from gecko.client import AgentApiClient, IntegrityError
from gecko.corpus import (
    ALLOWED_KEYS,
    ERROR_CLASSES,
    error_class_for,
    synthetic_sibling,
)
from gecko.surfaces import tools_rev
from gecko.validator import validate_all

TXODDS = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"

SPEC = {
    "openapi": "3.1.0",
    "servers": [{"url": "https://api.example.test"}],
    "paths": {
        "/odds/{fixtureId}": {
            "get": {
                "operationId": "get_odds",
                "summary": "Get odds",
                "parameters": [
                    {
                        "name": "fixtureId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        }
    },
}
SENSITIVE_FIXTURE = 4242424242


def _client(**kw) -> AgentApiClient:
    return AgentApiClient(SPEC, base_url="https://api.example.test", **kw)


# --- integrity -----------------------------------------------------------------------
def test_tools_rev_is_stable_and_content_sensitive():
    c = _client()
    assert tools_rev(c.tools) == c.tools_rev
    mutated = [dict(t) for t in c.tools]
    mutated[0]["description"] = "changed"
    assert tools_rev(mutated) != c.tools_rev


def test_tampered_tool_list_is_refused_at_prepare():
    c = _client()
    c.tools[0]["description"] = "tampered after comprehension"
    with pytest.raises(IntegrityError):
        c.prepare("get_odds", {"fixtureId": 1})


# --- corpus capture on client.call ---------------------------------------------------
def test_client_call_captures_metadata_only(tmp_path):
    path = tmp_path / "corpus.jsonl"
    c = _client(corpus_path=path)
    c.call("get_odds", {"fixtureId": SENSITIVE_FIXTURE}, mode="recorded")
    # Recorded mode fabricates a 200 -> synthetic -> segregated file, never the main corpus.
    synthetic = synthetic_sibling(path)
    assert not path.exists()
    raw = synthetic.read_text()
    rec = json.loads(raw.strip())
    assert set(rec) == ALLOWED_KEYS  # full allowlisted record, nothing extra
    assert str(SENSITIVE_FIXTURE) not in raw  # the arg VALUE never persisted
    assert rec["path_template"] == "/odds/{fixtureId}"  # template, not filled URL
    assert rec["error_class"] == "none"
    assert rec["mode"] == "recorded"
    assert rec["source"] == "synthetic"  # the faked 200 is labeled, not counted as real


def test_client_call_captures_preflight_callerror(tmp_path):
    path = tmp_path / "corpus.jsonl"
    c = _client(corpus_path=path)
    with pytest.raises(CallError):
        c.call("get_odds", {}, mode="recorded")  # missing required path param
    # A recorded-mode pre-flight failure is still synthetic (no wire status).
    rec = json.loads(synthetic_sibling(path).read_text().strip())
    assert rec["status"] is None
    assert rec["error_class"] == "missing_required_param"
    assert rec["source"] == "synthetic"


def test_no_capture_without_corpus_path(tmp_path):
    c = _client()  # no corpus_path
    c.call("get_odds", {"fixtureId": 1}, mode="recorded")
    assert not list(tmp_path.iterdir())  # nothing written


# --- corpus capture on the validator -------------------------------------------------
def test_validate_all_captures_outcomes(tmp_path):
    path = tmp_path / "corpus.jsonl"
    c = _client()
    validate_all(c, corpus_path=path)
    # The validator never calls upstream -> synthetic -> segregated by file, so a reader
    # of the main corpus never mistakes a synthetic bootstrap row for real usage.
    assert not path.exists()
    lines = [json.loads(x) for x in synthetic_sibling(path).read_text().splitlines()]
    assert lines
    for rec in lines:
        assert set(rec) == ALLOWED_KEYS
        assert rec["status"] is None  # pre-flight: no upstream call
        assert rec["source"] == "synthetic"


# --- the auth_host_blocked outcome class ---------------------------------------------
def test_auth_host_blocked_is_a_closed_error_class():
    assert "auth_host_blocked" in ERROR_CLASSES


def test_auth_host_refusal_maps_to_auth_host_blocked():
    with pytest.raises(CallError) as ei:
        build_request(
            {
                "name": "t",
                "inputSchema": {"type": "object", "properties": {}},
                "_invoke": {"method": "GET", "path": "/x", "param_locations": {}},
            },
            {},
            base_url="https://evil.test",
            auth={"Authorization": "SECRET"},
            allowed_auth_hosts={"api.legit.test"},
        )
    assert error_class_for(None, ei.value) == "auth_host_blocked"
    assert "SECRET" not in str(ei.value)
