"""Paying for a run: the quote, the preflight, the one capture BEFORE the run.

Payment is captured once, at the price the user
confirmed, and must land before any paid upstream call. A run that then delivers no fresh
or repaired fact earns a ``run_credit`` equal to what it cost, taken off the next quote;
it is a ledger line, never a transfer. A charge broadcast but not yet visible stays
``pending`` with its signature and is re-checked (:meth:`Billing.recheck`), never charged
again. There is no balance: the discount is worked out from ledger entries every time.

The maths here is pure. :class:`FakeBilling` touches no chain and is what recorded mode
and the tests run; ``SolanaBilling`` (lane 3) answers the same Protocol.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
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
    "FakeBilling",
    "LEDGER_SCAN_LIMIT",
    "Preflight",
    "QuoteOverCapError",
    "available_discount",
    "build_quote",
    "credit_undelivered",
    "iso_plus",
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


def _new_id() -> str:
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


@dataclass
class FakeBilling:
    """No chain. Always lands unless scripted to fail or to stay unseen; can be scripted
    to rebate. ``unseen_capture`` is a broadcast the chain does not show yet: the charge
    stays ``pending`` with its signature until ``recheck`` runs with it turned off.

    A SOL charge that lands is followed by a fake ``settlement`` entry (option B, 9 Oct),
    the transaction a rebate is matched to.

    ``wallets`` maps customer -> (payer address, USDC micro it holds); a customer not in
    it has no wallet, which is the honest answer while wallet creation is off.
    ``rebates`` maps run_id -> lamports the relay is seen to gain in that run's
    settlement slot.
    """

    config: AnyQueryConfig
    store: Store
    wallets: dict[str, tuple[str, int]] = field(default_factory=dict)
    fail_capture: bool = False
    unseen_capture: bool = False
    #: Signatures ``recheck`` looked up, in order.
    rechecks: list[str] = field(default_factory=list)
    rebates: dict[str, int] = field(default_factory=dict)
    sol_usdc_rate_micro: int = 150_000_000  # 150 USDC per SOL
    clock: Callable[[], str] = lambda: "2026-10-08T00:00:00Z"
    new_id: Callable[[], str] = _new_id
    _slot: int = 0

    def quote(self, plan: QueryPlan, *, now: str) -> Quote:
        held = self.store.ledger(plan.customer, limit=LEDGER_SCAN_LIMIT)
        return build_quote(
            plan,
            self.config,
            available=available_discount(held),
            quote_id=self.new_id(),
            now=now,
        )

    def preflight(self, customer: str, quote: Quote, pay_with: PayWith) -> Preflight:
        wallet = self.wallets.get(customer)
        if wallet is None:
            return Preflight(False, None, "no wallet bound; wallet creation is off")
        payer, held = wallet
        if pay_with == "USDC" and held < quote.total_usdc_micro:
            need = quote.total_usdc_micro / 1_000_000
            return Preflight(
                False, payer, f"needs {need:.2f} USDC, has {held / 1_000_000:.2f}"
            )
        return Preflight(True, payer, "the wallet can cover the quote")

    def _entry(self, payment: Payment, **overrides: object) -> LedgerEntry:
        base = LedgerEntry(
            entry_id=self.new_id(),
            customer=payment.customer,
            kind="charge",
            run_id=payment.run_id,
            quote_id=payment.quote_id,
            amount_usdc_micro=payment.amount_usdc_micro,
            status="pending",
            pay_with=payment.pay_with,
            signature=None,
            slot=None,
            send_label="fake",
            rebate_requested=False,
            rebate_lamports=None,
            split_user_pct=None,
            sol_usdc_rate_micro=None,
            created_at=self.clock(),
            note="",
        )
        return replace(base, **overrides)  # type: ignore[arg-type]

    def capture(self, payment: Payment) -> LedgerEntry:
        if payment.amount_usdc_micro < 0 or payment.discount_usdc_micro < 0:
            raise BillingError("payment amounts are never negative")
        if payment.amount_usdc_micro == 0:
            # The discount covered the whole quote: nothing moves, the discount is spent.
            entry = self._entry(
                payment,
                status="not_charged",
                rebate_requested=False,
                note="the discount covered the quote; nothing to transfer",
            )
            self.store.append_ledger(entry)
            spend_discount(self.store, entry, payment.discount_usdc_micro, self.new_id)
            return entry
        pending = self._entry(payment)
        self.store.append_ledger(pending)
        self._slot += 1
        signature = f"fake-{pending.entry_id}"
        if self.fail_capture:
            settled = replace(pending, status="failed", note="fake capture failed")
        elif self.unseen_capture:
            settled = replace(
                pending,
                signature=signature,
                note="broadcast, not yet visible; re-checked by signature",
            )
        else:
            settled = self._landed(pending, signature)
        self.store.update_ledger(settled)
        if settled.status == "landed":
            spend_discount(
                self.store, settled, payment.discount_usdc_micro, self.new_id
            )
            self._settle(settled)
        return settled

    def _settle(self, charge: LedgerEntry) -> None:
        """The fake settlement of a landed SOL charge: Gecko's own swap, never the user's."""
        if charge.pay_with != "SOL":
            return
        self._slot += 1
        entry_id = self.new_id()
        self.store.append_ledger(
            replace(
                charge,
                entry_id=entry_id,
                kind="settlement",
                status="landed",
                signature=f"fake-settle-{entry_id}",
                slot=self._slot,
                send_label="fake",
                rebate_requested=True,
                sol_usdc_rate_micro=self.sol_usdc_rate_micro,
                note=f"settles payment {charge.signature}",
            )
        )

    def _landed(self, pending: LedgerEntry, signature: str) -> LedgerEntry:
        rate = self.sol_usdc_rate_micro if pending.pay_with == "SOL" else None
        return replace(
            pending,
            status="landed",
            signature=signature,
            slot=self._slot,
            sol_usdc_rate_micro=rate,
            note="",
        )

    def recheck(self, entry: LedgerEntry) -> LedgerEntry:
        if entry.status != "pending" or entry.signature is None:
            return entry
        self.rechecks.append(entry.signature)
        if self.unseen_capture:
            return entry
        settled = self._landed(entry, entry.signature)
        self.store.update_ledger(settled)
        quote = self.store.quote(entry.customer, entry.quote_id or "")
        spend_discount(
            self.store, settled, quote.discount_usdc_micro if quote else 0, self.new_id
        )
        self._settle(settled)
        return settled

    def observe_rebates(self, customer: str) -> list[LedgerEntry]:
        held = self.store.ledger(customer, limit=LEDGER_SCAN_LIMIT)
        credited = {e.run_id for e in held if e.kind == "rebate_credit"}
        split = self.config.rebate_split_user_pct or 0
        written: list[LedgerEntry] = []
        for swap in held:
            if (
                swap.kind != "settlement"
                or swap.status != "landed"
                or not swap.rebate_requested
                or swap.run_id in credited
                or swap.run_id not in self.rebates
            ):
                continue
            lamports = self.rebates[swap.run_id]
            rate = swap.sol_usdc_rate_micro or 0
            credit = LedgerEntry(
                entry_id=self.new_id(),
                customer=customer,
                kind="rebate_credit",
                run_id=swap.run_id,
                quote_id=swap.quote_id,
                amount_usdc_micro=rebate_credit_micro(lamports, split, rate),
                status="landed",
                pay_with=None,
                signature=None,
                slot=swap.slot,
                send_label="fake",
                rebate_requested=False,
                rebate_lamports=lamports,
                split_user_pct=split,
                sol_usdc_rate_micro=rate,
                created_at=self.clock(),
                note="rebate observed in the settlement swap's slot",
            )
            self.store.append_ledger(credit)
            written.append(credit)
        return written

    def entries(self, customer: str, *, limit: int = 50) -> list[LedgerEntry]:
        return self.store.ledger(customer, limit=limit)
