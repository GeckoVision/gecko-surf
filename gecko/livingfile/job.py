"""The living file for one xStock: plan (free), then run (one paid call, confirmed).

    plan_file -> the mint, the balance and the share-price quote; spends nothing
    run_file  -> refuses without ``confirm=True``; reads the mint on-chain, Jupiter and
                 the legal pages (free), the share price (one capped Orthogonal call
                 through the shared runner), settles every fact against its last stored
                 version, computes the checks, and writes the table, Parquet, receipt
                 and one field-ledger row per call and per fact

A failed source costs its facts, not the run: they go stale (last good value kept and
marked) or "no data". A failure that would repeat (cap, credits, key) stops the paid
call, and the run still writes what the free sources gave.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..orthogonal.errors import (
    ListJobError,
    NotConfirmedError,
    ParquetUnavailableError,
)
from ..orthogonal.spec import BALANCE_OP, FD_PRICE_SNAPSHOT
from ..orthogonal.spend import SpendGuard
from ..orthogonal.supplier.history import Prior, priors
from ..orthogonal.supplier.runner import Runner
from . import checks, store
from .chain import read_mint
from .deps import XstockDeps
from .facts import RESERVES, FactRow, Reading, settle
from .legal import read_legal
from .market import read_market, read_share_price, share_price_call
from .readings import (
    chain_readings,
    legal_reading,
    market_readings,
    reserves_reading,
    share_reading,
)
from .plan import plan_file
from .xstocks import XStock, host_of, jupiter_url, xstock_for


@dataclass
class XstockResult:
    run_id: str
    mint: str
    symbol: str
    #: Model-safe rows: code-built values and our words, never page text.
    rows: list[dict[str, Any]]
    checks: dict[str, str]
    table_path: str
    parquet_path: str | None
    receipt_path: str
    receipt: dict[str, Any]
    notices: list[str] = field(default_factory=list)
    error: str | None = None
    code: str | None = None
    footer: str = (
        "Checks are computed in code. A red is a reason to look, not a finding. "
        "Gecko sends nothing."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: The keys a tool result carries per row. ``value_summary`` (page text) is not one.
SAFE_KEYS = (
    "field",
    "kind",
    "value",
    "model_summary",
    "source",
    "fetched_at",
    "status",
    "check",
    "changed",
    "previous_value",
    "previous_run_id",
    "note",
    "stale_since",
)


def safe_row(row: FactRow | dict[str, Any]) -> dict[str, Any]:
    raw = asdict(row) if isinstance(row, FactRow) else row
    return {key: raw.get(key) for key in SAFE_KEYS}


def _priors(deps: XstockDeps, notices: list[str]) -> dict[tuple[str, str], Prior]:
    try:
        rows = store.stored_rows(deps.base.folder)
    except ParquetUnavailableError:
        notices.append(
            "No earlier runs could be read (pyarrow missing), so no previous version "
            "is shown and nothing can be marked changed."
        )
        return {}
    return priors(rows, mode=deps.mode, subject_key=store.SUBJECT_KEY)


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def _readings(
    deps: XstockDeps, run: Runner[XstockDeps], xstock: XStock
) -> tuple[list[Reading], list[str]]:
    clock, sources = deps.base.clock, []
    mint = read_mint(deps.rpc_url, xstock.mint, deps.rpc_call, now_ts=clock())
    out = chain_readings(mint, rpc_host=host_of(deps.rpc_url), at=_iso(clock()))
    for step in mint.steps:
        run.public_ledger_row("solana-rpc", step, xstock.mint)
    market = read_market(
        jupiter_url(deps.jupiter_url, xstock.mint), xstock.mint, deps.public_fetch
    )
    run.public_ledger_row("jupiter", market.step, xstock.mint)
    out += market_readings(
        market, jupiter_host=host_of(deps.jupiter_url), at=_iso(clock())
    )
    fetched = run.call(share_price_call(xstock))
    payload = fetched.payloads[0] if fetched and fetched.payloads else None
    share = read_share_price(payload, xstock.ticker) if payload is not None else None
    why = run.stop.message if run.stop else "the paid call gave no reply"
    out.append(share_reading(share, xstock.ticker, _iso(clock()), why))
    for page in deps.legal_pages:
        legal = read_legal(page, deps.public_fetch)
        run.public_ledger_row("web", legal.step, xstock.mint)
        out.append(legal_reading(legal, _iso(clock())))
        sources.append(page.field)
    out.append(reserves_reading(deps.reserves(xstock), _iso(clock())))
    return out, sources


def run_file(deps: XstockDeps, mint: str, *, confirm: bool) -> XstockResult:
    if confirm is not True:
        raise NotConfirmedError(
            "Nothing was spent. Show the plan first, and run again with "
            "confirm=true only after the person says yes."
        )
    now = deps.base.clock()
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    plan = plan_file(deps, mint)
    xstock = xstock_for(plan.mint)
    base = deps.base
    run: Runner[XstockDeps] = Runner(
        deps,
        SpendGuard(base.ledger, base.per_run_cap, base.total_cap),
        {FD_PRICE_SNAPSHOT: plan.total_cents},
        run_id,
    )
    known = _priors(deps, run.notices)
    readings, legal_fields = _readings(deps, run, xstock)

    def settled(reading: Reading) -> FactRow:
        prior = known.get((xstock.mint, reading.field))
        return settle(
            reading, prior, mint=xstock.mint, symbol=xstock.symbol, run_id=run_id
        )

    rows = [settled(r) for r in readings]
    by_field = {r.field: r for r in rows}
    at = _iso(deps.base.clock())
    rows += [
        settled(checks.price_gap(by_field, deps.gap_threshold_pct, at)),
        settled(checks.supply_vs_reserves(by_field, at)),
        settled(checks.legal_changed([by_field[f] for f in legal_fields], at)),
    ]
    for row in rows:
        run.ledger_rows.append(
            {
                **run.ledger_base(),
                "kind": "field",
                "subject": xstock.mint,
                "field": row.field,
                "filled_by": row.source,
                "failover": False,
                "drift": "",
                "outcome": row.status,
            }
        )
    try:
        after = base.api.call(BALANCE_OP)
        balance_after = after.get("balance") if isinstance(after, dict) else None
    except ListJobError:
        balance_after = None
    run.notices.extend(deps.field_ledger.append(run.ledger_rows))
    verdicts: dict[str, str] = {
        r.field: r.check or "" for r in rows if r.kind == "check"
    }
    receipt = {
        "mode": deps.mode,
        "run_id": run_id,
        "mint": xstock.mint,
        "symbol": xstock.symbol,
        "calls": [asdict(c) for c in run.calls],
        "quoted_cents": plan.total_cents,
        "charged_cents": run.guard.spent,
        "per_run_cap_cents": base.per_run_cap,
        "total_spent_cents": base.ledger.total_cents(),
        "total_cap_cents": base.total_cap,
        "balance_before": plan.balance,
        "balance_after": balance_after if isinstance(balance_after, str) else None,
        "sources": sorted({r.source for r in rows}),
        "checks": verdicts,
        "statuses": {r.field: r.status for r in rows},
        "stopped": run.stop.code if run.stop else None,
    }
    written = store.write(
        base.folder,
        run_id,
        xstock.symbol,
        rows,
        receipt,
        mode=deps.mode,
        generated_at=_iso(now),
    )
    run.notices.extend(written.notices)
    if by_field[RESERVES].status == "no data":
        run.notices.append(f"Reserves not checked: {by_field[RESERVES].note}")
    return XstockResult(
        run_id=run_id,
        mint=xstock.mint,
        symbol=xstock.symbol,
        rows=[safe_row(r) for r in rows],
        checks=verdicts,
        table_path=str(written.table_path),
        parquet_path=str(written.parquet_path) if written.parquet_path else None,
        receipt_path=str(written.receipt_path),
        receipt=receipt,
        notices=run.notices,
        error=run.stop.message if run.stop else None,
        code=run.stop.code if run.stop else None,
    )
