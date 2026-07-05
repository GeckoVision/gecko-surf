"""funnel.py — the "installed but didn't use" + dev-retention funnel, per surface.

This is the demo-day evidence: of the external devs who ran ``claude mcp add`` and
whose client actually CONNECTED (an MCP ``initialize``), how many made a first call,
and how many came back. The funnel, per surface, over a window:

    connects  ->  connect_failed  ->  activated (>=1 call)  ->  returned (>=2 calls)

It reads ONLY ``gecko_events.surf_events`` — the same control-plane-clean metadata
``gecko.events`` was allowed to write (event kind, opaque surface id, sanitized client
label, opaque session id). There is no payload/arg-value to read because none was ever
stored. Retention is a true per-session join: a ``surf.call`` carries the same opaque
``session_id`` the transport assigned at ``surf.connect``.

EXTERNAL-only by default: OUR own clients (and any name in ``GECKO_SELF_CLIENTS``) are
excluded, so the numbers are honestly "N external devs connected, M activated, K
returned" — not us testing our own server.

Graceful + read-only: with ``MONGODB_URI`` unset and no ``--jsonl`` it prints a
friendly note and exits 0 (a plain OSS checkout never phones home to read, either).

    uv run python scripts/funnel.py                     # Mongo, last 30 days
    uv run python scripts/funnel.py --days 7
    uv run python scripts/funnel.py --jsonl events.jsonl # local fallback, no Mongo
    GECKO_SELF_CLIENTS="my-smoke-test" uv run python scripts/funnel.py

NOTE: capturing live connects needs the served app REDEPLOYED with the initialize hook
(gecko/http_server.py) — connects are only emitted by the running HTTP surface.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# Single source of truth for the collection location + the event vocabulary.
from gecko.events import EVENTS_COLLECTION, EVENTS_DB

# Our own client names — a connect/call from one of these is US, not an external dev.
# Matched case-insensitively as a name prefix (a client is "name/version"). Extend via
# the GECKO_SELF_CLIENTS env (comma-separated) without a code change.
_DEFAULT_SELF_CLIENTS = frozenset(
    {"gecko", "gecko-surf", "surfcall", "gecko-smoke", "mcp-inspector"}
)

_CALL_EVENTS = frozenset({"surf.call", "surf.search"})


@dataclass(frozen=True)
class FunnelRow:
    """One surface's funnel over the window. Every field is a COUNT of external,
    de-duplicated sessions — never a raw value."""

    surface_id: str
    connects: int  # distinct external sessions that completed initialize
    connect_failed: int  # failed initialize handshakes (external, best-effort)
    activated: int  # distinct external sessions with >=1 tool call
    returned: int  # distinct external sessions with >=2 tool calls (retention)

    @property
    def activation_rate(self) -> float | None:
        return self.activated / self.connects if self.connects else None

    @property
    def retention_rate(self) -> float | None:
        return self.returned / self.activated if self.activated else None


def _is_self(client: Any, self_norm: frozenset[str]) -> bool:
    """True if a client label is one of OURS (case-insensitive name-prefix match)."""
    if not isinstance(client, str) or not client:
        return False
    lowered = client.lower()
    return any(lowered == s or lowered.startswith(s) for s in self_norm)


def summarize_funnel(
    events: list[dict[str, Any]], self_clients: frozenset[str] = _DEFAULT_SELF_CLIENTS
) -> list[FunnelRow]:
    """Pure aggregation: events -> one ``FunnelRow`` per surface, EXTERNAL only.

    Self-attribution flows through the session id: a session whose ``surf.connect``
    carried one of our own client labels is marked self, and its later calls (which
    carry no client of their own) are excluded too. Sessions we only ever see calling
    (no connect in-window) are still counted as external activity — an honest floor.

    A ``surf.call`` with no ``session_id`` (aggregate fallback / a legacy row) cannot be
    attributed to a session, so it is excluded from the per-session activated/returned
    math rather than silently inflating it.
    """
    self_norm = frozenset(c.lower() for c in self_clients)

    surfaces: set[str] = set()
    # surface -> {session_id: client}
    connect_client: dict[str, dict[str, Any]] = defaultdict(dict)
    self_sessions: dict[str, set[str]] = defaultdict(set)
    failed: dict[str, int] = defaultdict(int)
    # surface -> {session_id: call_count}
    call_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for event in events:
        kind = event.get("event")
        surface = event.get("surface_id")
        if not isinstance(surface, str) or not surface:
            continue
        surfaces.add(surface)
        client = event.get("client")
        session_id = event.get("session_id")
        if kind == "surf.connect":
            if isinstance(session_id, str) and session_id:
                connect_client[surface][session_id] = client
                if _is_self(client, self_norm):
                    self_sessions[surface].add(session_id)
        elif kind == "surf.connect_failed":
            if not _is_self(client, self_norm):
                failed[surface] += 1
        elif kind in _CALL_EVENTS:
            if isinstance(session_id, str) and session_id:
                call_counts[surface][session_id] += 1

    rows: list[FunnelRow] = []
    for surface in sorted(surfaces):
        selfset = self_sessions[surface]
        ext_connect_sessions = {
            sid
            for sid, client in connect_client[surface].items()
            if not _is_self(client, self_norm)
        }
        ext_calls = {
            sid: n for sid, n in call_counts[surface].items() if sid not in selfset
        }
        rows.append(
            FunnelRow(
                surface_id=surface,
                connects=len(ext_connect_sessions),
                connect_failed=failed[surface],
                activated=sum(1 for n in ext_calls.values() if n >= 1),
                returned=sum(1 for n in ext_calls.values() if n >= 2),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Loading — Mongo (reuses events infra) or a local JSONL fallback
# --------------------------------------------------------------------------- #
def _mongo_uri() -> str | None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
    return None if not uri or uri == "__unset__" else uri


def load_from_mongo(days: int) -> list[dict[str, Any]] | None:
    """Read ``surf_events`` over the last ``days``. Returns ``None`` when ``MONGODB_URI``
    is unset or ``pymongo`` is absent (a plain OSS checkout). Read-only."""
    uri = _mongo_uri()
    if not uri:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    cutoff = int(time.time() * 1000) - days * 86_400_000
    try:
        coll = MongoClient(uri, serverSelectionTimeoutMS=3000)[EVENTS_DB][
            EVENTS_COLLECTION
        ]
        return list(coll.find({"ts": {"$gte": cutoff}}))
    except Exception:  # noqa: BLE001 - read-only view; a Mongo hiccup is not fatal
        return None


def load_from_jsonl(path: str, days: int) -> list[dict[str, Any]]:
    """Read events from a local JSONL fallback (one surf-event doc per line), filtered
    to the last ``days``. Best-effort: a malformed line is skipped."""
    cutoff = int(time.time() * 1000) - days * 86_400_000
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(doc, dict):
                    continue
                ts = doc.get("ts")
                if isinstance(ts, int) and ts < cutoff:
                    continue
                out.append(doc)
    except OSError:
        return []
    return out


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def _pct(rate: float | None) -> str:
    return "  n/a" if rate is None else f"{rate * 100:>4.0f}%"


def render(rows: list[FunnelRow], days: int, source: str) -> str:
    lines = [
        f"surfcall funnel — external devs, last {days}d  (source: {source})",
        "=" * 78,
        "landing -> install -> CONNECT (initialize) -> first call -> repeat call",
        "",
        f"{'surface':<24}{'connect':>8}{'failed':>8}{'activ.':>8}"
        f"{'return':>8}{'act%':>7}{'ret%':>7}",
        "-" * 78,
    ]
    if not rows:
        lines.append("  (no events in window)")
    for row in rows:
        lines.append(
            f"{row.surface_id[:24]:<24}{row.connects:>8}{row.connect_failed:>8}"
            f"{row.activated:>8}{row.returned:>8}"
            f"{_pct(row.activation_rate):>7}{_pct(row.retention_rate):>7}"
        )
    if rows:
        lines += [
            "-" * 78,
            f"{'TOTAL':<24}{sum(r.connects for r in rows):>8}"
            f"{sum(r.connect_failed for r in rows):>8}"
            f"{sum(r.activated for r in rows):>8}"
            f"{sum(r.returned for r in rows):>8}",
        ]
    lines += [
        "",
        "connect  = external clients that completed the MCP initialize handshake",
        "failed   = initialize handshakes that 4xx'd (stale-session clients)",
        "activ.   = of those, sessions that made >=1 tool call  (installed AND used)",
        "return   = sessions that made >=2 tool calls           (came back)",
    ]
    return "\n".join(lines)


def _self_clients_from_env() -> frozenset[str]:
    extra = os.environ.get("GECKO_SELF_CLIENTS", "")
    names = {n.strip().lower() for n in extra.split(",") if n.strip()}
    return _DEFAULT_SELF_CLIENTS | names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="surfcall funnel: connect -> activate -> return, per surface ($0, read-only)"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="window in days (default 30)"
    )
    parser.add_argument(
        "--jsonl", default=None, help="read events from a local JSONL file (no Mongo)"
    )
    args = parser.parse_args(argv)

    if args.jsonl:
        events = load_from_jsonl(args.jsonl, args.days)
        source = f"jsonl:{args.jsonl}"
    else:
        events = load_from_mongo(args.days)
        if events is None:
            print(
                "MONGODB_URI unset (and no --jsonl) — nothing to read.\n"
                "  Point MONGODB_URI at gecko_events, or pass --jsonl <file> for a "
                "local fallback."
            )
            return 0
        source = f"{EVENTS_DB}.{EVENTS_COLLECTION}"

    rows = summarize_funnel(events, self_clients=_self_clients_from_env())
    print(render(rows, args.days, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
