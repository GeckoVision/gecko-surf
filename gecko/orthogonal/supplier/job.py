"""The supplier watch: plan (free), then run (paid, only when confirmed).

    plan_watch -> the sheet, the balance, each endpoint's price; spends nothing
    run_watch  -> refuses without ``confirm=True``; per supplier, makes the planned
                  calls through the shared paginator and spend guard, repairs a mapped
                  field whose first source drifted or failed (``repair.py``), checks
                  the SDN list, computes agreement in code, writes the table, the
                  Parquet copy, the receipt, one field-ledger row per call and one per
                  mapped field

A failed call costs its field, not the run: the field reads "no data". A failure that
would repeat on every call (cap reached, credits out, key rejected, a follow-up page
that charged) stops the remaining calls, and the run still writes what it has.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import ListJobError, NotConfirmedError, ParquetUnavailableError
from ..spec import BALANCE_OP
from ..spend import SpendGuard
from .collect import watch_one
from .deps import SupplierDeps
from .drift import baseline_from
from .fields import AGREEMENTS, FieldRow, SupplierFacts, build_rows
from .history import Prior, priors
from .plan import WatchPlan, build_plan, windows
from .repair import RepairContext
from .repair_rows import repair_supplier, row_for
from .runner import Runner
from .sanctions import SanctionsListError, SdnList, load_sdn, screen
from .sources import SOURCE_MAP
from .store import read_runs, write_run


def plan_watch(deps: SupplierDeps, sheet: str) -> WatchPlan:
    """Everything the person needs to say yes or no. Spends nothing."""
    return build_plan(deps, sheet)[0]


@dataclass
class WatchResult:
    run_id: str
    #: Model-safe rows: our own summaries, never upstream text.
    rows: list[dict[str, Any]]
    agreement_counts: dict[str, int]
    table_path: str
    parquet_path: str | None
    receipt_path: str
    receipt: dict[str, Any]
    notices: list[str]
    #: One line per field that failed over, in our words (no upstream text).
    repairs: list[str] = field(default_factory=list)
    error: str | None = None
    code: str | None = None
    footer: str = (
        "Name matches and agreement are computed in code. A possible SDN match is not "
        "a finding; check it by hand. Gecko sends nothing."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _snapshots(deps: SupplierDeps, notices: list[str]) -> dict[tuple[str, str], Prior]:
    try:
        return priors(read_runs(deps.base.folder), mode=deps.mode)
    except ParquetUnavailableError:
        notices.append(
            "No earlier runs could be read (pyarrow missing), so a failover value "
            "is not compared with a last snapshot."
        )
        return {}


def _load_sdn(deps: SupplierDeps, notices: list[str]) -> SdnList | None:
    try:
        return load_sdn(
            deps.sdn_url,
            today=deps.today(),
            fetch=deps.sdn_fetch,
            cache_dir=deps.sdn_cache_dir,
            min_entries=deps.sdn_min_entries,
        )
    except SanctionsListError as exc:
        notices.append(f"Sanctions not checked: {exc.message}")
        return None


def run_watch(deps: SupplierDeps, sheet: str, *, confirm: bool) -> WatchResult:
    if confirm is not True:
        raise NotConfirmedError(
            "Nothing was spent. Show the plan first, and run again with "
            "confirm=true only after the person says yes."
        )
    now = deps.base.clock()
    run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    plan, suppliers, prices = build_plan(deps, sheet)
    base = deps.base
    run = Runner(
        deps,
        SpendGuard(base.ledger, base.per_run_cap, base.total_cap),
        prices,
        run_id,
        notices=list(plan.notices),
    )
    sdn = _load_sdn(deps, run.notices)
    month_start, since = windows(deps.today(), deps.website_lookback_days)
    ctx = RepairContext(
        run,
        baseline_from(deps.field_ledger.rows(), mode=deps.mode, before_run=run_id),
        _snapshots(deps, run.notices),
        deps.force_failover,
        deps.gleif_base_url,
        deps.gleif_fetch,
    )
    skip = frozenset(SOURCE_MAP[f].first.call_key for f in deps.force_failover)
    rows: list[FieldRow] = []
    repairs: list[str] = []
    for supplier in suppliers.suppliers:
        facts = SupplierFacts(supplier)
        replies, harvest = watch_one(run, facts, skip)
        resolved = repair_supplier(ctx, facts, replies, harvest)
        hit = screen(facts.sanctions_names, sdn) if sdn else None
        built = build_rows(
            facts, month_start=month_start, website_since=since, sdn=sdn, hit=hit
        )
        by_field = {r.field_name: r for r in resolved}
        rows.extend(
            row_for(by_field[r.field], r, run_id) if r.field in by_field else r
            for r in built
        )
        repairs.extend(
            f"{supplier.name}: {r.line()}" for r in resolved if r.trouble is not None
        )
    try:
        after = base.api.call(BALANCE_OP)
        balance_after = after.get("balance") if isinstance(after, dict) else None
    except ListJobError:
        balance_after = None
    run.notices.extend(deps.field_ledger.append(run.ledger_rows))
    if run.skipped:
        run.notices.append(f"{run.skipped} planned calls were not made.")
    receipt = {
        "mode": deps.mode,
        "run_id": run_id,
        "suppliers": len(suppliers.suppliers),
        "calls": [asdict(c) for c in run.calls],
        "calls_skipped": run.skipped,
        "quoted_cents": plan.total_cents,
        "charged_cents": run.guard.spent,
        "per_run_cap_cents": base.per_run_cap,
        "total_spent_cents": base.ledger.total_cents(),
        "total_cap_cents": base.total_cap,
        "balance_before": plan.balance,
        "balance_after": balance_after if isinstance(balance_after, str) else None,
        "sanctions_list": (
            {
                "source": sdn.source,
                "publish_date": sdn.publish_date,
                "sha256": sdn.sha256,
            }
            if sdn
            else None
        ),
        "agreement_counts": _counts(rows),
        "failovers": len(repairs),
        "stopped": run.stop.code if run.stop else None,
    }
    written = write_run(
        base.folder,
        run_id,
        rows,
        receipt,
        mode=deps.mode,
        generated_at=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
    )
    run.notices.extend(written.notices)
    return WatchResult(
        run_id=run_id,
        rows=[
            {
                "supplier": r.supplier,
                "field": r.field,
                "summary": r.model_summary,
                "source": r.source,
                "second_source": r.second_source,
                "agreement": r.agreement,
                "status": r.state,
                "filled_by": r.filled_by,
                "failover": r.failover,
            }
            for r in rows
        ],
        repairs=repairs,
        agreement_counts=_counts(rows),
        table_path=str(written.table_path),
        parquet_path=str(written.parquet_path) if written.parquet_path else None,
        receipt_path=str(written.receipt_path),
        receipt=receipt,
        notices=run.notices,
        error=run.stop.message if run.stop else None,
        code=run.stop.code if run.stop else None,
    )


def _counts(rows: list[FieldRow]) -> dict[str, int]:
    tally: dict[str, int] = {a: 0 for a in AGREEMENTS}
    for row in rows:
        tally[row.agreement] += 1
    return tally
