"""The surfcall⇄LLM seam (recorded, offline, $0 — Pattern B).

Proves the engine ingests the hand-authored stub, surfaces exactly the 5 public
read tools (auth hidden), encodes the canonical enum (first-call-correct), and that
`call` is a safe, non-raising, length-capped, allow-listed boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from examples.sos_vzla_bot.surfcall_tools import PUBLIC_READS, SurfcallTools

SPEC = Path(__file__).resolve().parents[1] / "spec" / "sosvenezuela_openapi.json"


def _tools(**kw) -> SurfcallTools:
    return SurfcallTools(SPEC, mode="recorded", **kw)


def test_exposes_exactly_the_five_public_reads():
    names = {t["name"] for t in _tools().tools_for_llm()}
    assert names == PUBLIC_READS
    assert names == {
        "getReports",
        "searchPersons",
        "getPersonStats",
        "getRecentDamage",
        "getNews",
    }
    assert "search_capabilities" not in names  # synthetic tool not exposed to the agent


def test_tools_have_llm_shape_and_hide_auth():
    for t in _tools().tools_for_llm():
        assert set(t) == {"name", "description", "input_schema"}  # Anthropic tool shape
        assert isinstance(t["input_schema"], dict)
        props = {p.lower() for p in t["input_schema"].get("properties", {})}
        assert not (props & {"authorization", "x-api-token", "x-api-key", "api-key"})


def test_search_persons_encodes_estado_enum_first_call_correct():
    tool = next(t for t in _tools().tools_for_llm() if t["name"] == "searchPersons")
    props = tool["input_schema"]["properties"]
    assert "q" in props
    assert props["estado"]["enum"] == ["seeking_info", "found_alive"]


def test_call_returns_sanitized_json_recorded():
    out = _tools().call("searchPersons", {"q": "Maria"})
    parsed = json.loads(out)  # must be valid JSON
    assert parsed["status"] == 200
    assert isinstance(parsed["data"], list)
    assert "request" not in parsed  # the filled URL is not echoed to the agent


def test_call_rejects_non_allowlisted_tool():
    out = _tools().call("getPersonStats_DROP_TABLE", {})
    assert json.loads(out)["error"]  # rejected, structured, no raise


def test_call_never_raises_and_caps_length():
    out = _tools(max_chars=200).call("getReports", {})
    assert isinstance(out, str)
    assert len(out) <= 200
