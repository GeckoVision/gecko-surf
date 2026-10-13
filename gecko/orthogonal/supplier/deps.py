"""Where recorded and live differ for the supplier watch — and nowhere else.

Recorded: the made-up Orthogonal on the transport seam, no key, the made-up SDN list,
in-memory spend and field ledgers. Live: the wire, the keyring key, the real SDN URL
behind the SSRF guard, and the persistent ledgers under ``~/.gecko/orthogonal/``.
Every other line of the job is shared.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ...access import AuthSession
from ...modes import CallMode
from ..api import Transport
from ..job import JobDeps, deps_for
from ..spend import FileLedger, Ledger, MemoryLedger
from .fixture import RecordedSupplierOrthogonal, made_up_sdn_fetch
from .gleif import (
    DEFAULT_GLEIF_BASE_URL,
    PublicFetch,
    check_base_url,
    gleif_live_fetch,
    made_up_gleif_fetch,
)
from .sanctions import DEFAULT_SDN_URL, MIN_ENTRIES, Fetch, live_fetch
from .sources import check_fields
from .store import FieldLedger, FileFieldLedger, MemoryFieldLedger, default_state_dir

#: Founder-approved 2026-10-08. Config, not law: pass other values to ``deps_for``.
WATCH_PER_RUN_CAP_CENTS = 60
WATCH_TOTAL_CAP_CENTS = 90
WEBSITE_LOOKBACK_DAYS = 30


@dataclass
class SupplierDeps:
    base: JobDeps
    field_ledger: FieldLedger
    sdn_fetch: Fetch
    sdn_cache_dir: Path | None
    sdn_url: str = DEFAULT_SDN_URL
    sdn_min_entries: int = MIN_ENTRIES
    website_lookback_days: int = WEBSITE_LOOKBACK_DAYS
    #: Fields whose FIRST source is not called this run, so the failover is
    #: exercised on purpose (``--force-failover entity``).
    force_failover: frozenset[str] = frozenset()
    gleif_base_url: str = DEFAULT_GLEIF_BASE_URL
    gleif_fetch: PublicFetch = made_up_gleif_fetch

    @property
    def mode(self) -> CallMode:
        return self.base.mode

    def today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self.base.clock(), dt.timezone.utc).date()


def _live_fetch(url: str) -> str:  # pragma: no cover - wire
    return live_fetch(url)


def _gleif_live(url: str) -> tuple[int, str]:  # pragma: no cover - wire
    return gleif_live_fetch(url)


def supplier_deps_for(
    mode: CallMode,
    *,
    folder: Path | None = None,
    state_dir: Path | None = None,
    ledger: Ledger | None = None,
    field_ledger: FieldLedger | None = None,
    transport: Transport | None = None,
    session: AuthSession | None = None,
    sdn_fetch: Fetch | None = None,
    sdn_url: str | None = None,
    clock: Callable[[], float] = time.time,
    per_run_cap: int = WATCH_PER_RUN_CAP_CENTS,
    total_cap: int = WATCH_TOTAL_CAP_CENTS,
    force_failover: Iterable[str] = (),
    gleif_base_url: str | None = None,
    gleif_fetch: PublicFetch | None = None,
) -> SupplierDeps:
    live = mode == "live"
    state = state_dir if state_dir is not None else default_state_dir()
    if ledger is None:
        # Its own total, so the commenter rehearsal and this demo cap independently.
        ledger = FileLedger(state / "supplier-ledger.json") if live else MemoryLedger()
    if transport is None and not live:
        today = dt.datetime.fromtimestamp(clock(), dt.timezone.utc).date()
        transport = RecordedSupplierOrthogonal(today=today)
    base = deps_for(
        mode, folder=folder, ledger=ledger, transport=transport, session=session
    )
    base.clock, base.per_run_cap, base.total_cap = clock, per_run_cap, total_cap
    return SupplierDeps(
        base=base,
        field_ledger=field_ledger
        or (FileFieldLedger(state) if live else MemoryFieldLedger()),
        sdn_fetch=sdn_fetch or (_live_fetch if live else made_up_sdn_fetch),
        sdn_cache_dir=state if live else None,
        sdn_url=sdn_url or DEFAULT_SDN_URL,
        sdn_min_entries=MIN_ENTRIES if live else 1,
        force_failover=check_fields(force_failover),
        gleif_base_url=check_base_url(gleif_base_url or DEFAULT_GLEIF_BASE_URL),
        gleif_fetch=gleif_fetch or (_gleif_live if live else made_up_gleif_fetch),
    )
