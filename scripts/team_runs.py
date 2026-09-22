"""team_runs.py — repeat runs per bootcamp team, week over week.

Each team gets its own unlisted mount, ``bootcamp-<team>`` (``GECKO_BOOTCAMP_TEAMS`` on
the hosted MCP), and the mount name is what ``surf_events`` already stores as
``surface_id``. So a team's activity is a filter over metadata we were already allowed to
write: event kind, mount, opaque session id, tool NAME. No argument value, no wallet, no
payload is read, because none was ever stored.

A RUN is a call to a tool that moves a job toward the chain: planning a payment, preparing
a purchase or an instruction, rehearsing, submitting. Reading a menu is not a run. Per
team and ISO week the report prints sessions that made a call, calls, runs, and whether
the team was also active the week before (the repeat signal).

Crawlers and our own clients are excluded exactly as ``funnel.py`` excludes them.

    uv run python scripts/team_runs.py                 # Mongo, last 8 weeks
    uv run python scripts/team_runs.py --weeks 4
    uv run python scripts/team_runs.py --jsonl events.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.uaclass import reclassify_client  # noqa: E402
from scripts.funnel import (  # noqa: E402
    _is_self,
    _self_clients_from_env,
    load_from_jsonl,
    load_from_mongo,
)

TEAM_PREFIX = "bootcamp-"
SHARED = "bootcamp"
RUN_TOOLS = frozenset(
    {
        "plan_payment",
        "prepare_purchase",
        "prepare_instruction",
        "try_purchase",
        "submit_transaction",
    }
)
_CALLS = frozenset({"surf.call", "surf.search"})


@dataclass(frozen=True)
class WeekRow:
    team: str
    week: str  # ISO year-week, e.g. 2026-W39
    sessions: int
    calls: int
    runs: int
    repeat: bool  # also active in the ISO week before


def _team(surface: Any) -> str | None:
    if surface == SHARED:
        return "(shared mount)"
    if isinstance(surface, str) and surface.startswith(TEAM_PREFIX):
        return surface[len(TEAM_PREFIX) :]
    return None


def _week(ts_ms: int) -> str:
    year, week, _ = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).isocalendar()
    return f"{year}-W{week:02d}"


def _previous(week: str) -> str:
    year, number = week.split("-W")
    monday = dt.date.fromisocalendar(int(year), int(number), 1) - dt.timedelta(days=7)
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def summarize(
    events: list[dict[str, Any]], self_clients: frozenset[str] = frozenset()
) -> list[WeekRow]:
    """Pure aggregation: events -> one row per (team, ISO week), external sessions only."""
    self_norm = frozenset(c.lower() for c in self_clients)
    excluded: set[str] = set()
    for event in events:
        if event.get("event") == "surf.connect" and _team(event.get("surface_id")):
            sid = event.get("session_id")
            if isinstance(sid, str) and (
                _is_self(event.get("client"), self_norm)
                or reclassify_client(event) == "robot"
            ):
                excluded.add(sid)

    sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    calls: dict[tuple[str, str], int] = defaultdict(int)
    runs: dict[tuple[str, str], int] = defaultdict(int)
    for event in events:
        team = _team(event.get("surface_id"))
        ts = event.get("ts")
        sid = event.get("session_id")
        if team is None or event.get("event") not in _CALLS or not isinstance(ts, int):
            continue
        if isinstance(sid, str) and sid in excluded:
            continue
        key = (team, _week(ts))
        calls[key] += 1
        if isinstance(sid, str) and sid:
            sessions[key].add(sid)
        if event.get("tool_name") in RUN_TOOLS:
            runs[key] += 1

    active = {key for key in calls if calls[key] > 0}
    return [
        WeekRow(
            team=team,
            week=week,
            sessions=len(sessions[(team, week)]),
            calls=calls[(team, week)],
            runs=runs[(team, week)],
            repeat=(team, _previous(week)) in active,
        )
        for team, week in sorted(calls)
    ]


def render(rows: list[WeekRow], source: str) -> str:
    if not rows:
        return f"no team activity in {source}"
    lines = [
        f"source: {source}",
        f"{'team':<20} {'week':<9} {'sessions':>8} {'calls':>6} {'runs':>5}  repeat",
    ]
    for row in rows:
        lines.append(
            f"{row.team:<20} {row.week:<9} {row.sessions:>8} {row.calls:>6} "
            f"{row.runs:>5}  {'yes' if row.repeat else '-'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--jsonl", default=None)
    args = parser.parse_args(argv)
    days = args.weeks * 7
    if args.jsonl:
        events, source = load_from_jsonl(args.jsonl, days), args.jsonl
    else:
        loaded = load_from_mongo(days)
        if loaded is None:
            print(
                "MONGODB_URI is unset (or pymongo absent); pass --jsonl for a local file"
            )
            return 0
        events, source = loaded, "gecko_events.surf_events"
    print(render(summarize(events, _self_clients_from_env()), source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
