"""``FakeBilling``: the :class:`~gecko.anyquery.charge.Billing` default with no chain.

Recorded mode and the tests run it. It never moves money and never holds a key.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .charge import (
    LEDGER_SCAN_LIMIT,
    BillingError,
    Preflight,
    available_discount,
    build_quote,
    new_entry_id,
    rebate_credit_micro,
    spend_discount,
)
from .config import AnyQueryConfig
from .store import Store
from .types import LedgerEntry, Payment, PayWith, QueryPlan, Quote

__all__ = ["FakeBilling"]


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
    new_id: Callable[[], str] = new_entry_id
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
