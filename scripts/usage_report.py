"""usage_report.py — calls per surface per day, distinct sessions, top tools.

Read-only. The logic lives in ``gecko.usage_report``; this parses arguments and picks
the source.

    MONGODB_URI=... uv run python scripts/usage_report.py                 # last 7 days
    MONGODB_URI=... uv run python scripts/usage_report.py --since 2026-09-14 --until 2026-09-28
    uv run python scripts/usage_report.py --json surf_events.json         # a mongoexport

The Mongo URI is read from the process environment only. This script does not open
``.env``; whoever runs it exports the variable.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

from gecko.usage_report import (
    UsageReportError,
    load_export,
    load_from_mongo,
    render,
    summarize_usage,
)


def _date(text: str) -> date:
    return date.fromisoformat(text)


def main(argv: list[str] | None = None) -> int:
    today = datetime.now(timezone.utc).date()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", type=_date, default=today - timedelta(days=6))
    parser.add_argument("--until", type=_date, default=today, help="inclusive")
    parser.add_argument("--json", help="read a mongoexport (array or JSONL) instead")
    args = parser.parse_args(argv)
    if args.until < args.since:
        print("--until is before --since", file=sys.stderr)
        return 2

    try:
        if args.json:
            docs = load_export(args.json)
            source = args.json
        else:
            uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
            if not uri:
                print(
                    "MONGODB_URI is not set and no --json export was given.",
                    file=sys.stderr,
                )
                return 2
            docs = load_from_mongo(uri, args.since, args.until)
            source = "gecko_events.surf_events"
    except UsageReportError as exc:
        print(f"usage report: {exc}", file=sys.stderr)
        return 1

    print(render(summarize_usage(docs, args.since, args.until), source=source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
