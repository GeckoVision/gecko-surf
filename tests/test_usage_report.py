"""The usage report: calls per surface per day, distinct sessions, top tools.

Runs offline against a JSON export — the same code path the Mongo read feeds.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from gecko.usage_report import (
    load_export,
    mongo_filter,
    render,
    summarize_usage,
)


def _ms(day: str, hour: int = 12) -> int:
    d = datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


DOCS: list[dict[str, Any]] = [
    # course, day 1: two sessions, three calls, one failed
    {
        "event": "surf.call",
        "ts": _ms("2026-09-26"),
        "surface_id": "course",
        "tool_name": "search_course",
        "session_id": "s1",
        "ok": True,
    },
    {
        "event": "surf.call",
        "ts": _ms("2026-09-26"),
        "surface_id": "course",
        "tool_name": "search_course",
        "session_id": "s2",
        "ok": True,
    },
    {
        "event": "surf.call",
        "ts": _ms("2026-09-26"),
        "surface_id": "course",
        "tool_name": "read_course_page",
        "session_id": "s1",
        "ok": False,
    },
    # course, day 2: returning session s1
    {
        "event": "surf.call",
        "ts": _ms("2026-09-27"),
        "surface_id": "course",
        "tool_name": "search_course",
        "session_id": "s1",
        "ok": True,
    },
    # orquestra, day 2, no session id
    {
        "event": "surf.call",
        "ts": _ms("2026-09-27"),
        "surface_id": "orquestra",
        "tool_name": "list_stores",
    },
    # not a call: excluded
    {
        "event": "surf.connect",
        "ts": _ms("2026-09-27"),
        "surface_id": "course",
        "session_id": "s9",
    },
    # outside the range: excluded
    {
        "event": "surf.call",
        "ts": _ms("2026-09-20"),
        "surface_id": "course",
        "tool_name": "search_course",
        "session_id": "old",
    },
]

START, END = date(2026, 9, 26), date(2026, 9, 27)


def test_calls_per_surface_per_day() -> None:
    report = summarize_usage(DOCS, START, END)
    assert report.calls_by_day == {
        ("course", "2026-09-26"): 3,
        ("course", "2026-09-27"): 1,
        ("orquestra", "2026-09-27"): 1,
    }
    assert report.total_calls == 5


def test_distinct_sessions_and_errors_per_surface() -> None:
    report = summarize_usage(DOCS, START, END)
    course = report.surfaces["course"]
    assert course.calls == 4
    assert course.sessions == 2  # s1 returned on day 2 and counts once
    assert course.errors == 1
    assert report.surfaces["orquestra"].sessions == 0
    assert report.distinct_sessions == 2


def test_top_tools_are_ranked() -> None:
    report = summarize_usage(DOCS, START, END)
    assert report.top_tools[0] == ("course", "search_course", 3)
    assert ("orquestra", "list_stores", 1) in report.top_tools


def test_mongo_filter_is_the_same_window() -> None:
    f = mongo_filter(START, END)
    assert f["event"] == "surf.call"
    assert f["ts"]["$gte"] == _ms("2026-09-26", 0)
    assert f["ts"]["$lt"] == _ms("2026-09-28", 0)  # END is inclusive


def test_load_export_reads_array_and_jsonl(tmp_path: Path) -> None:
    array = tmp_path / "a.json"
    array.write_text(json.dumps(DOCS[:2]), encoding="utf-8")
    lines = tmp_path / "b.jsonl"
    # mongoexport's canonical form wraps int64 as {"$numberLong": "..."}
    wrapped = dict(DOCS[0], ts={"$numberLong": str(DOCS[0]["ts"])})
    lines.write_text(json.dumps(wrapped) + "\n\nnot json\n", encoding="utf-8")
    assert len(load_export(array)) == 2
    (doc,) = load_export(lines)
    assert summarize_usage([doc], START, END).total_calls == 1


def test_render_names_every_number() -> None:
    text = render(summarize_usage(DOCS, START, END), source="test")
    assert "2026-09-26" in text and "course" in text and "search_course" in text
    assert "distinct sessions" in text


def _script() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "usage_report.py"
    spec = importlib.util.spec_from_file_location("usage_report_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_runs_offline_on_an_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    export = tmp_path / "events.json"
    export.write_text(json.dumps(DOCS), encoding="utf-8")
    code = _script().main(
        ["--json", str(export), "--since", "2026-09-26", "--until", "2026-09-27"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "course" in out and "orquestra" in out


def test_script_without_a_source_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGO_URI", raising=False)
    assert _script().main([]) == 2
    assert "MONGODB_URI" in capsys.readouterr().err
