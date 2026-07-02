"""adoption.py — the honest, $0, read-only weekly adoption view.

Two free signals, no infra to stand up:

* **PyPI installs** of ``gecko-surf`` (pypistats public API) — did anyone install it.
* **Usage events** in ``gecko_events.surf_events`` (last 7d) — did they actually USE
  it: accesses by event kind, unique surfaces, **repeat-access** (a surface hit >1×),
  and the **first-call-correct rate**. These are the flywheel signals — installs are
  vanity; repeat-access + first-call-correct are whether the comprehension compounds.

Control-plane-clean by inheritance: it reads only the metadata ``gecko.events`` was
allowed to write (event kind, opaque surface id, ok-bool, error class…). There is no
payload/arg-value to read because none was ever stored.

Graceful: with ``MONGODB_URI`` unset it prints the PyPI section only. Read-only —
never writes, never breaks on a network/Mongo hiccup.

    uv run python scripts/adoption.py            # last 7 days
    uv run python scripts/adoption.py --days 30
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

# Single source of truth for the collection location.
from gecko.events import EVENTS_COLLECTION, EVENTS_DB

_PYPISTATS_URL = "https://pypistats.org/api/packages/{package}/recent"
_HTTP_TIMEOUT_S = 10


# --------------------------------------------------------------------------- #
# PyPI installs (public, no auth)
# --------------------------------------------------------------------------- #
def fetch_pypi(package: str = "gecko-surf") -> dict[str, int] | None:
    """last_day / last_week / last_month download counts, or ``None`` on any failure
    (offline, 404 before first release, rate-limit). Read-only, best-effort."""
    url = _PYPISTATS_URL.format(package=package)
    if not url.startswith("https://pypistats.org/"):  # pin the host (no SSRF surface)
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "surfcall-adoption"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310 - pinned https host
            body = json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or {}
        return {
            "last_day": int(data.get("last_day", 0)),
            "last_week": int(data.get("last_week", 0)),
            "last_month": int(data.get("last_month", 0)),
        }
    except Exception:  # noqa: BLE001 - read-only view; degrade to "unavailable"
        return None


# --------------------------------------------------------------------------- #
# Usage events (gecko_events.surf_events)
# --------------------------------------------------------------------------- #
def _mongo_uri() -> str | None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    return None if not uri or uri == "__unset__" else uri


def fetch_usage(days: int = 7) -> dict[str, Any] | None:
    """Aggregate the flywheel signals from ``surf_events`` over the last ``days``.

    Returns ``None`` when ``MONGODB_URI`` is unset or ``pymongo`` is absent (a plain
    OSS checkout) — the caller then prints the PyPI-only view. Read-only aggregation."""
    uri = _mongo_uri()
    if not uri:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    cutoff = int(time.time() * 1000) - days * 86_400_000
    match = {"ts": {"$gte": cutoff}}
    try:
        coll = MongoClient(uri, serverSelectionTimeoutMS=3000)[EVENTS_DB][
            EVENTS_COLLECTION
        ]
        by_event = {
            str(d["_id"]): int(d["n"])
            for d in coll.aggregate(
                [{"$match": match}, {"$group": {"_id": "$event", "n": {"$sum": 1}}}]
            )
        }
        surface_counts = [
            int(d["n"])
            for d in coll.aggregate(
                [
                    {"$match": {**match, "surface_id": {"$ne": None}}},
                    {"$group": {"_id": "$surface_id", "n": {"$sum": 1}}},
                ]
            )
        ]
        fcc = {
            bool(d["_id"]): int(d["n"])
            for d in coll.aggregate(
                [
                    {"$match": {**match, "event": "surf.first_call_correct"}},
                    {"$group": {"_id": "$ok", "n": {"$sum": 1}}},
                ]
            )
        }
    except Exception:  # noqa: BLE001 - read-only view; a Mongo hiccup is not fatal
        return None

    fcc_total = sum(fcc.values())
    return {
        "total_events": sum(by_event.values()),
        "by_event": by_event,
        "unique_surfaces": len(surface_counts),
        "repeat_surfaces": sum(1 for n in surface_counts if n > 1),
        "fcc_ok": fcc.get(True, 0),
        "fcc_total": fcc_total,
        "fcc_rate": (fcc.get(True, 0) / fcc_total) if fcc_total else None,
    }


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
# Print order for the event kinds (stable, human-scannable).
_EVENT_ORDER = ("surf.search", "surf.prepare", "surf.call", "surf.first_call_correct")


def render(pypi: dict[str, int] | None, usage: dict[str, Any] | None, days: int) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        f"surfcall adoption — {today}",
        "=" * 60,
        "",
        "PyPI installs (gecko-surf)",
    ]
    if pypi is None:
        lines.append("  (unavailable — offline, rate-limited, or pre-first-release)")
    else:
        lines += [
            f"  last day:    {pypi['last_day']:>6}",
            f"  last week:   {pypi['last_week']:>6}",
            f"  last month:  {pypi['last_month']:>6}",
        ]

    lines += ["", f"Usage events ({EVENTS_DB}.{EVENTS_COLLECTION}, last {days}d)"]
    if usage is None:
        lines.append("  (MONGODB_URI unset — usage events skipped, PyPI only)")
        return "\n".join(lines)

    lines.append(f"  total events:        {usage['total_events']:>6}")
    by_event = usage["by_event"]
    for kind in _EVENT_ORDER:
        if kind in by_event:
            lines.append(f"    {kind:<24}{by_event[kind]:>6}")
    for kind in sorted(set(by_event) - set(_EVENT_ORDER)):  # any future kind
        lines.append(f"    {kind:<24}{by_event[kind]:>6}")
    lines.append(f"  unique surfaces:     {usage['unique_surfaces']:>6}")
    lines.append(
        f"  repeat-access:       {usage['repeat_surfaces']:>6}   (surfaces used >1x)"
    )
    if usage["fcc_rate"] is None:
        lines.append("  first-call-correct:   n/a   (no outcomes yet)")
    else:
        pct = usage["fcc_rate"] * 100
        lines.append(
            f"  first-call-correct:  {pct:>5.1f}%   "
            f"({usage['fcc_ok']}/{usage['fcc_total']})"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="surfcall adoption view ($0, read-only)"
    )
    parser.add_argument("--days", type=int, default=7, help="usage window (default 7)")
    parser.add_argument("--package", default="gecko-surf", help="PyPI package name")
    args = parser.parse_args(argv)
    pypi = fetch_pypi(args.package)
    usage = fetch_usage(args.days)
    print(render(pypi, usage, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
