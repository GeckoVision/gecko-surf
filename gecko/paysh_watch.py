"""Hourly self-refresh + drift-watch for the aggregated pay.sh catalog surface.

The aggregated pay.sh surface (:mod:`gecko.catalog_mcp`) is a snapshot of a catalog that
DRIFTS — endpoints move, paywalls die. This module keeps the SERVED surface honest while
it runs: on every tick it re-comprehends the catalog (Tier-1 sha-diff) and re-probes each
resolved endpoint challenge-only (Tier-2 402), MUTATING the registry in place so the tools
an agent sees reflect the fresh state without a redeploy.

Every drift TRANSITION is logged in a control-plane-safe, grep-friendly form — this is the
"pay.sh says X, Gecko caught the drift at HH:MM" evidence line::

    paysh drift: paysponge/coingecko verified→broken (probe=404) at 2026-07-11T14:00:00+00:00

Invariants (all upheld here):
  * Control plane only — we store surface + tool defs + correctness metadata
    ({sha, status, last_verified}); NEVER response payloads or secrets.
  * $0 / challenge-only — the probe reads status codes; it signs and settles nothing.
  * SSRF-safe — the injected ``fetch`` / ``probe`` both run ``validate_public_url`` first
    (``fetch_catalog`` / ``challenge_probe``); the catalog is treated as untrusted.
  * Resilient — a network error in fetch or probe is caught + logged and the loop
    continues to the next tick; it NEVER crashes the server.
  * The clock is injected (``now``) and threaded into ``drift_watch`` + the log timestamp,
    so freshness is deterministic and fakeable in tests (never a raw wall-clock call).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .paysh_catalog import (
    CatalogEntry,
    CatalogRegistry,
    DriftResult,
    ProbeFn,
    RefreshDiff,
)

logger = logging.getLogger("gecko.paysh_watch")

#: How often the self-refresh loop ticks. Env-overridable; sensible hourly default.
REFRESH_SECONDS_ENV = "PAYSH_REFRESH_SECONDS"
DEFAULT_REFRESH_SECONDS = 3600

#: A catalog fetcher with the URL already bound (``fetch_catalog`` fits: it defaults the
#: URL). Injectable so tests never touch the network.
Fetch = Callable[[], list[CatalogEntry]]
#: A monotonic-ish clock returning epoch seconds. Injected + threaded through, never called
#: implicitly, so tests can pin freshness.
Clock = Callable[[], float]
#: A transition sink (e.g. ``print`` for the CLI). The line is ALSO always logged.
Sink = Callable[[str], None]


def refresh_seconds(env: dict[str, str] | None = None) -> int:
    """The tick interval from ``PAYSH_REFRESH_SECONDS`` (positive int) or the default."""
    source = os.environ if env is None else env
    raw = source.get(REFRESH_SECONDS_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_REFRESH_SECONDS


@dataclass(frozen=True)
class TickResult:
    """One refresh+drift cycle's control-plane-safe summary (never a payload).

    ``fetch_failed`` marks a tick skipped because the catalog fetch raised — the loop
    caught it and will retry next tick.
    """

    refresh: RefreshDiff | None
    transitions: list[DriftResult]
    fetch_failed: bool = False


def _iso(now: Clock) -> str:
    return datetime.fromtimestamp(now(), tz=timezone.utc).isoformat()


def run_tick(
    registry: CatalogRegistry,
    *,
    fetch: Fetch,
    probe: ProbeFn,
    now: Clock = time.time,
    sink: Sink | None = None,
) -> TickResult:
    """One $0 control-plane tick: Tier-1 sha-diff refresh, then Tier-2 challenge-only
    drift-watch. Mutates ``registry`` IN PLACE so the served surface reflects the fresh
    state. Logs every drift TRANSITION (and mirrors it to ``sink``).

    A fetch OR probe-cycle failure is caught, logged, and returned as a no-op tick — it
    NEVER propagates, so the caller's loop can't die on a transient network blip.
    """
    try:
        entries = fetch()
    except Exception:  # noqa: BLE001 - a catalog fetch failure must not kill the loop
        logger.warning("paysh refresh: catalog fetch failed; skipping this tick")
        return TickResult(refresh=None, transitions=[], fetch_failed=True)
    try:
        diff = registry.refresh(entries)
        # Snapshot AFTER refresh (a re-comprehended provider is reset) but BEFORE the
        # probe, so a transition is reported against the current served status.
        before = {ps.entry.fqn: ps.status for ps in registry.providers()}
        results = registry.drift_watch(probe, now=now)
    except Exception:  # noqa: BLE001 - a drift-watch failure must not kill the loop
        logger.warning("paysh drift-watch: probe cycle failed; skipping this tick")
        return TickResult(refresh=None, transitions=[])
    ts = _iso(now)
    transitions = [r for r in results if r.changed]
    for r in transitions:
        old = before.get(r.fqn, "pending")
        line = f"paysh drift: {r.fqn} {old}→{r.status} (probe={r.probe_status}) at {ts}"
        logger.info(line)
        if sink is not None:
            sink(line)
    return TickResult(refresh=diff, transitions=transitions)


async def watch_loop(
    registry: CatalogRegistry,
    *,
    interval: float,
    fetch: Fetch,
    probe: ProbeFn,
    now: Clock = time.time,
    sink: Sink | None = None,
) -> None:
    """Run :func:`run_tick` forever, every ``interval`` seconds (tick-first), until
    cancelled (server shutdown).

    Each tick runs in a worker thread (``asyncio.to_thread``) — ``fetch``/``probe`` do
    blocking network I/O and must not stall the event loop. ``asyncio.CancelledError``
    from ``asyncio.sleep`` propagates out cleanly for a graceful shutdown.
    """
    while True:
        await asyncio.to_thread(
            run_tick, registry, fetch=fetch, probe=probe, now=now, sink=sink
        )
        await asyncio.sleep(interval)


__all__ = [
    "DEFAULT_REFRESH_SECONDS",
    "REFRESH_SECONDS_ENV",
    "TickResult",
    "refresh_seconds",
    "run_tick",
    "watch_loop",
]
