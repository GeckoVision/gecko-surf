"""The charge seam: what the runner needs from whoever takes payment for a run.

The engine never decides how a run is paid. It asks an injected :class:`Billing` for a
quote, a preflight and one capture, and :class:`~gecko.anyquery.charge_fake.FakeBilling`
is the default that touches no chain. ``SolanaBilling`` answers the same Protocol on chain.

Payment is captured once, at the price the user
confirmed, and must land before any paid upstream call. A run that then delivers no fresh
or repaired fact earns a ``run_credit`` equal to what it cost, taken off the next quote;
it is a ledger line, never a transfer. A charge broadcast but not yet visible stays
``pending`` with its signature and is re-checked (:meth:`Billing.recheck`), never charged
again. There is no balance: the discount is worked out from ledger entries every time.

The maths here is pure.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from .calls import distinct_calls
from .config import AnyQueryConfig
from .store import DuplicateChargeError, Store
from .types import (
    USDC_MICRO_PER_CENT,
    LedgerEntry,
    Payment,
    PayWith,
    QueryPlan,
    Quote,
    QuotePriceFrom,
)

__all__ = [
    "Billing",
    "BillingError",
    "LEDGER_SCAN_LIMIT",
    "Preflight",
    "QuoteOverCapError",
    "available_discount",
    "build_quote",
    "credit_undelivered",
    "iso_plus",
    "new_entry_id",
    "rebate_credit_micro",
    "run_credit_entry",
    "spend_discount",
]

#: How many ledger entries the discount maths reads. The Store has no aggregate, and a
#: customer with more entries than this would under-count their discount.
LEDGER_SCAN_LIMIT = 10_000


class BillingError(Exception):
    """Redacted: never carries a key, a URL or a wallet_id."""


class QuoteOverCapError(BillingError):
    """The run's worst-case total is over ``max_run_usdc_micro``."""


@dataclass(frozen=True)
class Preflight:
    ok: bool
    payer: str | None
    reason: str  # our words


class Billing(Protocol):
    def quote(self, plan: QueryPlan, *, now: str) -> Quote: ...
    def preflight(
        self, customer: str, quote: Quote, pay_with: PayWith
    ) -> Preflight: ...
    def capture(self, payment: Payment) -> LedgerEntry: ...
    def recheck(self, entry: LedgerEntry) -> LedgerEntry: ...
    def observe_rebates(self, customer: str) -> list[LedgerEntry]: ...
    def entries(self, customer: str, *, limit: int = 50) -> list[LedgerEntry]: ...


def available_discount(entries: Iterable[LedgerEntry]) -> int:
    """Credits (observed rebates, undelivered runs) minus discounts already spent.
    Never negative."""
    credit = spent = 0
    for entry in entries:
        if entry.kind in ("rebate_credit", "run_credit"):
            credit += entry.amount_usdc_micro
        elif entry.kind == "discount" and entry.status == "landed":
            spent += entry.amount_usdc_micro
    return max(credit - spent, 0)


def run_credit_entry(
    *,
    entry_id: str,
    customer: str,
    run_id: str,
    quote_id: str,
    amount_usdc_micro: int,
    created_at: str,
    note: str,
) -> LedgerEntry:
    """The credit an undelivered run earns: what its charge and its discount cost,
    given back as a discount on the next quote. No signature: nothing moves on chain."""
    if amount_usdc_micro <= 0:
        raise BillingError("a run credit is always positive")
    return LedgerEntry(
        entry_id=entry_id,
        customer=customer,
        kind="run_credit",
        run_id=run_id,
        quote_id=quote_id,
        amount_usdc_micro=amount_usdc_micro,
        status="landed",
        pay_with=None,
        signature=None,
        slot=None,
        send_label=None,
        rebate_requested=False,
        rebate_lamports=None,
        split_user_pct=None,
        sol_usdc_rate_micro=None,
        created_at=created_at,
        note=note,
    )


def credit_undelivered(
    store: Store,
    customer: str,
    run_id: str,
    *,
    new_id: Callable[[], str],
    now: str,
    note: str,
) -> LedgerEntry | None:
    """Give an undelivered run's cost back as a ``run_credit``: its landed charge plus
    the discount it used, so a credit spent on a run that delivered nothing is not lost.

    Idempotent: one credit per run (the store's unique key), so a retried settle returns
    the credit already written. ``None`` when the run cost nothing.
    """
    entries = [
        e for e in store.ledger(customer, limit=LEDGER_SCAN_LIMIT) if e.run_id == run_id
    ]
    for held in entries:
        if held.kind == "run_credit":
            return held
    cost = sum(
        e.amount_usdc_micro
        for e in entries
        if e.kind in ("charge", "discount") and e.status == "landed"
    )
    if cost <= 0:
        return None
    charge = next((e for e in entries if e.kind == "charge"), None)
    credit = run_credit_entry(
        entry_id=new_id(),
        customer=customer,
        run_id=run_id,
        quote_id=charge.quote_id if charge and charge.quote_id else "",
        amount_usdc_micro=cost,
        created_at=now,
        note=note,
    )
    try:
        store.append_ledger(credit)
    except DuplicateChargeError:
        # A concurrent settle wrote it first; that one is the credit.
        return next(
            e
            for e in store.ledger(customer, limit=LEDGER_SCAN_LIMIT)
            if e.run_id == run_id and e.kind == "run_credit"
        )
    return credit


def rebate_credit_micro(lamports: int, split_user_pct: int, rate_micro: int) -> int:
    """``floor(lamports * split/100 * rate_micro / 1e9)``, in integers only.

    ``rate_micro`` is micro-USDC per SOL, taken from the payment transaction itself.
    """
    if lamports < 0 or rate_micro < 0 or not 0 <= split_user_pct <= 100:
        raise BillingError("rebate inputs out of range")
    return lamports * split_user_pct * rate_micro // (100 * 1_000_000_000)


def iso_plus(now: str, seconds: int) -> str:
    """``now`` + ``seconds`` as an ISO-8601 UTC string ending in ``Z``."""
    moment = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    later = (moment + dt.timedelta(seconds=seconds)).astimezone(dt.UTC)
    return later.strftime("%Y-%m-%dT%H:%M:%SZ")


def _price_from(plan: QueryPlan) -> QuotePriceFrom:
    seen = {s.price_from for f in plan.fields for s in f.sources}
    if seen == {"live"}:
        return "live"
    if seen == {"pinned"} or not seen:
        return "pinned"
    return "mixed"


def build_quote(
    plan: QueryPlan,
    config: AnyQueryConfig,
    *,
    available: int,
    quote_id: str,
    now: str,
) -> Quote:
    """The worst-case price of one run of ``plan``: every DISTINCT request it could
    make, as if each rank-0 source failed over to the last one. Fields that read one
    endpoint for one subject share one call at run time, so they are priced once."""
    calls = distinct_calls(plan)
    upstream_cents = sum(calls.values())
    upstream_micro = upstream_cents * USDC_MICRO_PER_CENT
    fee = max(
        config.gecko_fee_floor_usdc_micro,
        upstream_micro * config.gecko_margin_pct // 100,
    )
    subtotal = upstream_micro + fee
    discount = min(max(available, 0), subtotal)
    total = subtotal - discount
    if total > config.max_run_usdc_micro:
        raise QuoteOverCapError(
            f"this run could cost {total} micro-USDC, over the "
            f"{config.max_run_usdc_micro} per-run ceiling; narrow the query"
        )
    return Quote(
        quote_id=quote_id,
        plan_id=plan.plan_id,
        customer=plan.customer,
        upstream_max_cents=upstream_cents,
        upstream_max_usdc_micro=upstream_micro,
        gecko_fee_usdc_micro=fee,
        discount_usdc_micro=discount,
        total_usdc_micro=total,
        max_calls=len(calls),
        price_from=_price_from(plan),
        created_at=now,
        expires_at=iso_plus(now, config.quote_ttl_s),
    )


def new_entry_id() -> str:
    """A random ledger or quote id. Not a secret: it only has to be unique."""
    return secrets.token_hex(8)


def spend_discount(
    store: Store,
    charge: LedgerEntry,
    discount_usdc_micro: int,
    new_id: Callable[[], str],
) -> LedgerEntry | None:
    """Record the discount a settled charge used. Written once the charge has landed (or
    needed no transfer), so a payment that never landed never spends a credit."""
    if discount_usdc_micro <= 0 or charge.status not in ("landed", "not_charged"):
        return None
    entry = replace(
        charge,
        entry_id=new_id(),
        kind="discount",
        amount_usdc_micro=discount_usdc_micro,
        status="landed",
        pay_with=None,
        signature=None,
        slot=None,
        rebate_requested=False,
        sol_usdc_rate_micro=None,
        note="credit applied to this run",
    )
    store.append_ledger(entry)
    return entry
