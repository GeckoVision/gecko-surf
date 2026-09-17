"""The run recorder and the graph drawn from it. Control plane only, by construction."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from gecko.trace import Trace, TraceRow, short
from scripts.trace_to_graph import spec_from_trace


class _Refused(Exception):
    def __init__(self) -> None:
        super().__init__("no")
        self.code = "instruction-altered"


def test_a_step_records_its_duration_and_the_facts_the_body_attached() -> None:
    trace = Trace(lane="rehearsal", network="fork")
    with trace.step("prepare", "gecko") as facts:
        facts["units"] = 54_647
        facts["binding_prefix"] = short("48A4282E2AE0EBE2ffff")
    row = trace.rows[0]
    assert row.step == "prepare" and row.party == "gecko" and row.outcome == "ok"
    assert row.units == 54_647 and row.binding_prefix == "48A4282E"
    assert row.ms >= 0 and row.seq == 0


def test_a_refusal_is_recorded_by_its_code_and_re_raised() -> None:
    trace = Trace(lane="rehearsal", network="fork")
    with pytest.raises(_Refused):
        with trace.step("sponsor", "relay"):
            raise _Refused()
    assert trace.rows[0].outcome == "instruction-altered"
    assert trace.refused is trace.rows[0]


def test_a_row_has_no_field_that_could_hold_a_payload() -> None:
    """The allowlist IS the dataclass: every field is a short scalar."""
    names = {f.name for f in fields(TraceRow)}
    assert names == {
        "seq",
        "step",
        "party",
        "outcome",
        "ms",
        "units",
        "binding_prefix",
        "note",
    }
    assert short("4jccRjipEL8CWje6a9PhEjKAnHgfSS1xTMqsf14KvAT7") == "4jccRjip"
    assert short(None) is None and short("") is None


def test_jsonl_round_trips(tmp_path) -> None:
    trace = Trace(lane="settle", network="mainnet")
    trace.record("sponsor", "relay", note="appended")
    trace.record("sign", "buyer", "authority-lamports-moved", ms=3)
    path = trace.write(tmp_path / "run.jsonl")
    lines = path.read_text().splitlines()
    assert json.loads(lines[0])["lane"] == "settle"
    back = Trace.read(path)
    assert back.network == "mainnet" and back.rows == trace.rows


def test_the_graph_marks_a_refusal_with_its_code_and_stops_there() -> None:
    trace = Trace(lane="rehearsal", network="fork")
    trace.record("fund", "gecko", ms=12)
    trace.record("prepare", "gecko", ms=800, units=54_647, binding_prefix="48A4282E")
    trace.record("sponsor", "relay", "instruction-altered", ms=40)
    trace.record("reset", "gecko", ms=5)
    spec = spec_from_trace(trace)
    assert spec["diagram_type"] == "sequence"
    assert [p["id"] for p in spec["participants"]] == ["run", "gecko", "relay"]
    messages = spec["messages"]
    assert [m["id"] for m in messages] == ["s0_fund", "s1_prepare", "s2_sponsor"]
    assert messages[1]["label"] == "prepare · 800 ms · 54,647 CU · 48A4282E"
    assert messages[2]["variant"] == "security"
    assert messages[2]["note"] == "refused: instruction-altered"
    assert messages[2]["to"] == "relay"
    assert "refused: instruction-altered" in spec["meta"]["title"]
    assert all(m["y"] >= 160 for m in messages)


def test_a_landed_run_draws_every_step() -> None:
    trace = Trace(lane="rehearsal", network="fork")
    for step, party in [
        ("fund", "gecko"),
        ("prepare", "gecko"),
        ("sponsor", "relay"),
        ("resimulate", "node"),
        ("cosign", "buyer"),
        ("land", "node"),
        ("judge", "gecko"),
        ("reset", "gecko"),
    ]:
        trace.record(step, party)
    spec = spec_from_trace(trace)
    assert len(spec["messages"]) == 8
    assert all(m["variant"] == "default" for m in spec["messages"])
    assert spec["meta"]["title"].endswith("landed")
