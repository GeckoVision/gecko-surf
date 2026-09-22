"""Per-team repeat runs: one mount per team, rows by ISO week, robots and our own out."""

from __future__ import annotations

import datetime as dt

import pytest

from gecko.serve_mcp import bootcamp_team_mounts
from scripts.team_runs import summarize


def _ms(year: int, month: int, day: int) -> int:
    return int(dt.datetime(year, month, day, 12, tzinfo=dt.UTC).timestamp() * 1000)


def _connect(surface: str, sid: str, client: str = "claude-ai") -> dict:
    return {
        "event": "surf.connect",
        "surface_id": surface,
        "session_id": sid,
        "client": client,
        "ts": _ms(2026, 9, 21),
    }


def _call(surface: str, sid: str, tool: str, ts: int) -> dict:
    return {
        "event": "surf.call",
        "surface_id": surface,
        "session_id": sid,
        "tool_name": tool,
        "ts": ts,
    }


def test_a_team_that_comes_back_the_next_week_is_a_repeat() -> None:
    week_38, week_39 = _ms(2026, 9, 16), _ms(2026, 9, 22)
    rows = summarize(
        [
            _connect("bootcamp-alpha", "s1"),
            _call("bootcamp-alpha", "s1", "list_stores", week_38),
            _call("bootcamp-alpha", "s1", "prepare_purchase", week_38),
            _call("bootcamp-alpha", "s2", "prepare_purchase", week_39),
            _call("bootcamp-alpha", "s2", "try_purchase", week_39),
            _call("bootcamp-beta", "s3", "list_stores", week_39),
            _call("orquestra", "s4", "prepare_purchase", week_39),  # not a team mount
        ]
    )
    by = {(r.team, r.week): r for r in rows}
    assert set(by) == {
        ("alpha", "2026-W38"),
        ("alpha", "2026-W39"),
        ("beta", "2026-W39"),
    }
    assert (by["alpha", "2026-W38"].runs, by["alpha", "2026-W38"].repeat) == (1, False)
    assert (by["alpha", "2026-W39"].runs, by["alpha", "2026-W39"].repeat) == (2, True)
    assert by["beta", "2026-W39"].runs == 0, "reading a menu is not a run"


def test_robots_and_our_own_clients_are_not_a_team() -> None:
    week = _ms(2026, 9, 22)
    rows = summarize(
        [
            _connect("bootcamp-alpha", "bot", client="verifymcp/1.0"),
            _call("bootcamp-alpha", "bot", "prepare_purchase", week),
            _connect("bootcamp-alpha", "us", client="gecko-smoke"),
            _call("bootcamp-alpha", "us", "prepare_purchase", week),
        ],
        self_clients=frozenset({"gecko-smoke"}),
    )
    assert rows == []


def test_team_mounts_come_from_the_env_and_drop_bad_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GECKO_BOOTCAMP_TEAMS", "alpha, Beta ,alpha,bad slug,../x,gamma-2"
    )
    assert bootcamp_team_mounts() == (
        "bootcamp-alpha",
        "bootcamp-beta",
        "bootcamp-gamma-2",
    )
    monkeypatch.delenv("GECKO_BOOTCAMP_TEAMS")
    assert bootcamp_team_mounts() == ()
