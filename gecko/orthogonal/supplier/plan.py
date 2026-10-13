"""The free step: what the watch will call, for how much, before anything is paid.

It reads the sheet, the balance and each endpoint's price from ``/v1/details`` (all
free), then refuses — still free — when the expected total would cross the per-run
cap, the total cap, or the balance.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..errors import OutOfCreditsError, SpendCapError
from ..extract import balance_cents, details_price_cents
from ..spec import BALANCE_OP, DETAILS_OP, PL_COMPANY, SUPPLIER_TARGETS, Target
from .calls import planned_calls
from .deps import SupplierDeps
from .sheet import SupplierSheet, read_suppliers
from .sources import SOURCE_MAP, failover_targets


@dataclass
class SupplierQuote:
    name: str
    domain: str
    ticker: str | None
    #: (endpoint, cents) per planned call, in call order.
    calls: list[tuple[str, int]]
    cents: int


@dataclass
class WatchPlan:
    suppliers: list[SupplierQuote]
    total_cents: int
    #: Every supplier needing the fallback name lookup AND every mapped field failing
    #: over to each paid alternative. The run cap still stops it first.
    worst_case_cents: int
    per_run_cap_cents: int
    total_left_cents: int
    balance: str | None
    sdn_url: str
    #: Per mapped field, its ranked sources and what each costs per supplier.
    failover_sources: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    message: str = ""


def windows(today: dt.date, lookback_days: int) -> tuple[dt.date, dt.date]:
    """(news since: first of this month, website changes since: ``lookback`` ago)."""
    return today.replace(day=1), today - dt.timedelta(days=lookback_days)


def quotes(deps: SupplierDeps) -> dict[Target, int]:
    """Each endpoint's price: the live ``/v1/details`` quote, never below the pin."""
    out: dict[Target, int] = {}
    for target in SUPPLIER_TARGETS:
        body: dict[str, Any] = {"api": target.api, "path": target.path}
        quoted = details_price_cents(deps.base.api.call(DETAILS_OP, body))
        out[target] = max(target.pinned_cents, quoted or 0)
    return out


def failover_prices(prices: dict[Target, int]) -> list[dict[str, Any]]:
    return [
        {
            "field": fs.field,
            "sources": [
                {
                    "rank": rank,
                    "source": src.name,
                    "cents": prices.get(src.target, src.price_cents)
                    if src.target
                    else src.price_cents,
                }
                for rank, src in enumerate(fs.sources, start=1)
            ],
        }
        for fs in SOURCE_MAP.values()
    ]


def _failover_words(prices: dict[Target, int]) -> str:
    parts = []
    for row in failover_prices(prices):
        steps = " -> ".join(
            f"{s['source'].split()[0]} ${s['cents'] / 100:.2f}" for s in row["sources"]
        )
        parts.append(f"{row['field']}: {steps}")
    return "; ".join(parts)


def build_plan(
    deps: SupplierDeps, sheet_name: str
) -> tuple[WatchPlan, SupplierSheet, dict[Target, int]]:
    # The sheet first: a wrong file is refused before any call, even a free one.
    sheet = read_suppliers(sheet_name, deps.base.folder)
    balance_raw = deps.base.api.call(BALANCE_OP)
    balance_text = balance_raw.get("balance") if isinstance(balance_raw, dict) else None
    prices = quotes(deps)
    month_start, since = windows(deps.today(), deps.website_lookback_days)
    lines: list[SupplierQuote] = []
    for s in sheet.suppliers:
        calls = [
            (c.endpoint, prices[c.target])
            for c in planned_calls(s, month_start=month_start, website_since=since)
        ]
        lines.append(
            SupplierQuote(s.name, s.domain, s.ticker, calls, sum(c for _e, c in calls))
        )
    total = sum(q.cents for q in lines)
    extra = failover_targets() | {PL_COMPANY}
    worst = total + sum(prices[t] for t in extra) * len(lines)
    base = deps.base
    left = base.total_cap - base.ledger.total_cents()
    if total > base.per_run_cap:
        raise SpendCapError(
            f"This sheet needs about ${total / 100:.2f}; one run is capped at "
            f"${base.per_run_cap / 100:.2f}. Nothing was spent. Remove a supplier."
        )
    if total > left:
        raise SpendCapError(
            f"This sheet needs about ${total / 100:.2f}; ${left / 100:.2f} is left "
            f"of the ${base.total_cap / 100:.2f} total. Nothing was spent."
        )
    have = balance_cents(balance_raw)
    if have is not None and have < total:
        raise OutOfCreditsError(str(balance_text), total)
    plan = WatchPlan(
        suppliers=lines,
        total_cents=total,
        worst_case_cents=worst,
        per_run_cap_cents=base.per_run_cap,
        total_left_cents=left,
        balance=balance_text if isinstance(balance_text, str) else None,
        sdn_url=deps.sdn_url,
        failover_sources=failover_prices(prices),
        notices=list(sheet.notices),
        message=(
            f"{len(lines)} suppliers, about ${total / 100:.2f}; worst case "
            f"${worst / 100:.2f} if every supplier needs the fallback name lookup and "
            "every mapped field fails over to its next source "
            f"({_failover_words(prices)}). Predictleads follow-up pages should be "
            "$0.00 and Gecko stops if one is not. Cap "
            f"${base.per_run_cap / 100:.2f} per run, ${left / 100:.2f} left of "
            f"${base.total_cap / 100:.2f}. The sanctions check is free. Continue?"
        ),
    )
    return plan, sheet, prices
