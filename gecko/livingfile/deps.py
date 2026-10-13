"""Where recorded and live differ for the living file — and nowhere else.

Recorded: a made-up chain, a made-up Jupiter and legal pages, the made-up Orthogonal on
the transport seam, no key, in-memory spend and field ledgers. Live: the RPC and web
reads through ``wire`` (netguard), the keyring key, the wire to Orthogonal, and the
persistent ledgers under ``~/.gecko/orthogonal/`` — its own files, so this job's cap
and drift baseline never mix with the supplier watch's. Every other line is shared.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..access import AuthSession
from ..modes import CallMode
from ..orthogonal.api import Transport
from ..orthogonal.job import JobDeps, deps_for
from ..orthogonal.spend import FileLedger, Ledger, MemoryLedger
from ..orthogonal.supplier.store import (
    FieldLedger,
    FileFieldLedger,
    MemoryFieldLedger,
    default_state_dir,
)
from ..rpc import RpcCall
from .checks import ReservesReader, reserves_not_checked
from .fixture import MadeUpChain, MadeUpWeb, RecordedXstockOrthogonal
from .wire import PublicFetch, live_public_fetch, live_rpc_call
from .xstocks import (
    DEFAULT_GAP_THRESHOLD_PCT,
    DEFAULT_JUPITER_URL,
    DEFAULT_RPC_URL,
    LEGAL_PAGES,
    LegalPage,
    check_https_url,
)

#: One $0.025 call fits; two never do.
XSTOCK_PER_RUN_CAP_CENTS = 5
XSTOCK_TOTAL_CAP_CENTS = 20
LEDGER_FILE = "xstock-ledger.json"
FIELD_LEDGER_STEM = "xstock-field-ledger"


@dataclass
class XstockDeps:
    base: JobDeps
    field_ledger: FieldLedger
    rpc_url: str
    rpc_call: RpcCall
    jupiter_url: str
    public_fetch: PublicFetch
    legal_pages: tuple[LegalPage, ...]
    gap_threshold_pct: Decimal
    reserves: ReservesReader

    @property
    def mode(self) -> CallMode:
        return self.base.mode


def xstock_deps_for(
    mode: CallMode,
    *,
    folder: Path | None = None,
    state_dir: Path | None = None,
    ledger: Ledger | None = None,
    field_ledger: FieldLedger | None = None,
    transport: Transport | None = None,
    session: AuthSession | None = None,
    rpc_url: str | None = None,
    rpc_call: RpcCall | None = None,
    jupiter_url: str | None = None,
    public_fetch: PublicFetch | None = None,
    legal_pages: tuple[LegalPage, ...] = LEGAL_PAGES,
    clock: Callable[[], float] = time.time,
    per_run_cap: int = XSTOCK_PER_RUN_CAP_CENTS,
    total_cap: int = XSTOCK_TOTAL_CAP_CENTS,
    gap_threshold_pct: Decimal = DEFAULT_GAP_THRESHOLD_PCT,
    reserves: ReservesReader | None = None,
) -> XstockDeps:
    live = mode == "live"
    state = state_dir if state_dir is not None else default_state_dir()
    if ledger is None:
        ledger = FileLedger(state / LEDGER_FILE) if live else MemoryLedger()
    if transport is None and not live:
        transport = RecordedXstockOrthogonal()
    base = deps_for(
        mode, folder=folder, ledger=ledger, transport=transport, session=session
    )
    base.clock, base.per_run_cap, base.total_cap = clock, per_run_cap, total_cap
    for page in legal_pages:
        check_https_url(page.url, page.label)
    return XstockDeps(
        base=base,
        field_ledger=field_ledger
        or (FileFieldLedger(state, FIELD_LEDGER_STEM) if live else MemoryFieldLedger()),
        rpc_url=check_https_url(rpc_url or DEFAULT_RPC_URL, "Solana RPC"),
        rpc_call=rpc_call or (live_rpc_call if live else MadeUpChain()),
        jupiter_url=jupiter_url or DEFAULT_JUPITER_URL,
        public_fetch=public_fetch or (live_public_fetch if live else MadeUpWeb()),
        legal_pages=legal_pages,
        gap_threshold_pct=gap_threshold_pct,
        reserves=reserves or reserves_not_checked,
    )
