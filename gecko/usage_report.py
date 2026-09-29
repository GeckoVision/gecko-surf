"""Who used which surface: calls per surface per day, distinct sessions, top tools.

Reads only ``surf.call`` records from ``gecko_events.surf_events``, which hold metadata
and nothing else (see ``gecko.events``). There is no argument or result to report
because none was ever stored.

Counts ``surf.call`` only. Every served call emits exactly one: ``McpSurface`` from its
own ``call_tool``, every other surface from the transport (``gecko.surface_calls``).
Before 2026-09-28 the second group emitted nothing, so earlier days under-count the
course and orquestra mounts to zero.

Read-only by construction: the loaders ``find``; nothing here writes.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .events import EVENTS_COLLECTION, EVENTS_DB

CALL_EVENT = "surf.call"
_DAY_MS = 86_400_000
TOP_TOOLS = 10


class UsageReportError(Exception):
    """The report could not read its source."""


@dataclass(frozen=True)
class SurfaceUsage:
    calls: int
    sessions: int
    errors: int


@dataclass(frozen=True)
class UsageReport:
    start: date
    end: date  # inclusive
    calls_by_day: dict[tuple[str, str], int]
    surfaces: dict[str, SurfaceUsage]
    top_tools: list[tuple[str, str, int]]
    total_calls: int
    distinct_sessions: int
    days: list[str] = field(default_factory=list)


def _day_start_ms(day: date) -> int:
    return int(
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def window_ms(start: date, end: date) -> tuple[int, int]:
    """``[start 00:00 UTC, end+1 00:00 UTC)`` in epoch ms — ``end`` is inclusive."""
    return _day_start_ms(start), _day_start_ms(end + timedelta(days=1))


def mongo_filter(start: date, end: date) -> dict[str, Any]:
    lo, hi = window_ms(start, end)
    return {"event": CALL_EVENT, "ts": {"$gte": lo, "$lt": hi}}


def _ts(value: Any) -> int | None:
    # mongoexport's canonical extended JSON wraps int64 as {"$numberLong": "..."}.
    if isinstance(value, dict):
        value = value.get("$numberLong", value.get("$date"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def summarize_usage(docs: list[dict[str, Any]], start: date, end: date) -> UsageReport:
    lo, hi = window_ms(start, end)
    by_day: Counter[tuple[str, str]] = Counter()
    calls: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    tools: Counter[tuple[str, str]] = Counter()
    sessions: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        if doc.get("event") != CALL_EVENT:
            continue
        ts = _ts(doc.get("ts"))
        if ts is None or not lo <= ts < hi:
            continue
        surface = str(doc.get("surface_id") or "unknown")
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
        by_day[(surface, day)] += 1
        calls[surface] += 1
        if doc.get("ok") is False:
            errors[surface] += 1
        tools[(surface, str(doc.get("tool_name") or "unknown"))] += 1
        session = doc.get("session_id")
        if isinstance(session, str) and session:
            sessions[surface].add(session)
    all_sessions = set().union(*sessions.values()) if sessions else set()
    return UsageReport(
        start=start,
        end=end,
        calls_by_day=dict(sorted(by_day.items())),
        surfaces={
            s: SurfaceUsage(calls=n, sessions=len(sessions[s]), errors=errors[s])
            for s, n in sorted(calls.items())
        },
        top_tools=[
            (s, t, n)
            for (s, t), n in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[
                :TOP_TOOLS
            ]
        ],
        total_calls=sum(calls.values()),
        distinct_sessions=len(all_sessions),
        days=sorted({d for _, d in by_day}),
    )


def load_export(path: str | Path) -> list[dict[str, Any]]:
    """A ``mongoexport`` of ``surf_events``: a JSON array or one document per line.

    A malformed line is skipped rather than failing the report, since an export is
    often hand-trimmed."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageReportError(f"cannot read export {path}: {exc.strerror}") from exc
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise UsageReportError(f"export {path} is not valid JSON") from exc
        return [d for d in data if isinstance(d, dict)]
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def load_from_mongo(uri: str, start: date, end: date) -> list[dict[str, Any]]:
    """Read the window's ``surf.call`` docs. Read-only; projection keeps it lean."""
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise UsageReportError("pymongo is not installed (the `events` extra)") from exc
    projection = {
        "_id": 0,
        "event": 1,
        "ts": 1,
        "surface_id": 1,
        "tool_name": 1,
        "session_id": 1,
        "ok": 1,
    }
    try:
        client: Any = MongoClient(uri, serverSelectionTimeoutMS=5000)
        coll = client[EVENTS_DB][EVENTS_COLLECTION]
        return list(coll.find(mongo_filter(start, end), projection))
    except Exception as exc:  # noqa: BLE001 - the URI carries a secret; never echo it
        raise UsageReportError(f"mongo read failed ({type(exc).__name__})") from exc


def render(report: UsageReport, *, source: str) -> str:
    lines = [
        f"surf.call usage {report.start} .. {report.end} (UTC, inclusive) from {source}",
        f"total calls: {report.total_calls}   distinct sessions: {report.distinct_sessions}",
        "",
        f"{'surface':<24} {'calls':>7} {'sessions':>9} {'errors':>7}",
    ]
    for name, usage in report.surfaces.items():
        lines.append(
            f"{name:<24} {usage.calls:>7} {usage.sessions:>9} {usage.errors:>7}"
        )
    lines += ["", "calls per surface per day:"]
    for (name, day), n in report.calls_by_day.items():
        lines.append(f"  {day}  {name:<24} {n:>7}")
    lines += ["", f"top tools (up to {TOP_TOOLS}):"]
    for name, tool, n in report.top_tools:
        lines.append(f"  {n:>7}  {name}/{tool}")
    if not report.total_calls:
        lines += ["", "no surf.call records in this window"]
    return "\n".join(lines)


__all__ = [
    "SurfaceUsage",
    "UsageReport",
    "UsageReportError",
    "load_export",
    "load_from_mongo",
    "mongo_filter",
    "render",
    "summarize_usage",
    "window_ms",
]
