"""The reference runner: the bounded loop that makes the scenarios passable.

This is the deterministic state machine a client runtime (LangGraph store
agent, Hermes, a SendAI agent, Claude) is measured against: browse -> gate the
plan -> for each item, RE-READ state, gate the spend, spend once, read the
receipt. It exists so the scenario pack has a proven-passable path BEFORE any
probabilistic runtime is graded — Pattern B: this whole loop is falsifiable
offline with a fake surface.

What the runner does NOT do: parse natural language (that is the runtime under
test's job — the runner takes an already-resolved :class:`OrderPlan`), hold a
key, or submit anything itself. The surface seam does the spending; live
implementations put PayBox behind it (autonomous credential mode — a passkey
wait does not fit a prepare window — prepare late, prepare once).

Sequencing rules, stated once:
* state is re-read immediately before EVERY spend (freshness is per-purchase,
  not per-session);
* one landed purchase per planned slot — the idempotency counter makes a
  retry-after-landed a refusal, not a second spend;
* the first blocked spend aborts the rest of the order; anything that landed
  before the block is REPORTED in the outcome, never hidden;
* gate reasons become the block reason verbatim — the gate and the grader
  share vocabulary, so surfacing the gate's words is what passing looks like.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from gecko.semantic_gate import (
    GateDecision,
    LiveItemState,
    OrderConstraints,
    ProposedPurchase,
    plan_gate,
    purchase_gate,
)
from gecko.semantic_grader import OutcomeRecord, PurchaseRecord
from gecko.semantic_outcome import blocked_outcome, executed_outcome


@dataclass(frozen=True)
class OrderPlan:
    """An already-resolved order: concrete item ids + typed constraints."""

    item_ids: tuple[str, ...]
    constraints: OrderConstraints


@dataclass(frozen=True)
class SpendResult:
    """What one spend produced, receipt-derived (see gecko.semantic_outcome)."""

    landed: bool
    purchase: PurchaseRecord | None


class StoreSurface(Protocol):
    """The seam a live implementation fills (fork sandbox, hosted MCP, PayBox).

    ``authority`` must be the store's on-chain authority as READ from the
    store account (the gecko.store_accounts seam) — never configuration an
    agent or a pasted message supplied.
    """

    def authority(self) -> str: ...

    def read_item(self, item_id: str) -> LiveItemState: ...

    def spend(self, proposed: ProposedPurchase) -> SpendResult: ...


class RunnerError(Exception):
    """Raised when the surface breaks its contract (fails the run, not silently)."""


def run_order(plan: OrderPlan, surface: StoreSurface) -> OutcomeRecord:
    """Execute an order through the gate. Returns the gradable terminal state."""
    decision: GateDecision = plan_gate(plan.item_ids, plan.constraints)
    if not decision.allow:
        return blocked_outcome(decision.reason_text())

    authority = surface.authority()
    planned_counts = Counter(plan.item_ids)
    landed: list[PurchaseRecord] = []
    purchased: Counter[str] = Counter()

    for item_id in plan.item_ids:
        live = surface.read_item(item_id)  # re-read immediately before the spend
        proposed = ProposedPurchase(
            item_id=item_id,
            quoted_price_lamports=live.price_lamports,
            destination=authority,
        )
        spend_decision = purchase_gate(
            proposed,
            live,
            store_authority=authority,
            already_purchased=purchased,
            max_count_for_item=planned_counts[item_id],
            price_change_approved=plan.constraints.user_approved,
        )
        if not spend_decision.allow:
            return blocked_outcome(
                spend_decision.reason_text(), purchases=tuple(landed)
            )

        result = surface.spend(proposed)
        if not result.landed or result.purchase is None:
            # Fail-closed: an unlanded spend ends the order as a named block —
            # retrying here is how the duplicate-receipt trap gets sprung.
            return blocked_outcome(
                f"spend for {item_id!r} did not land; stopping rather than retrying "
                "blind — a retry without a landed-check is a double-purchase risk.",
                purchases=tuple(landed),
            )
        if result.purchase.destination != authority:
            raise RunnerError(
                "surface reported a landed spend credited to "
                f"{result.purchase.destination!r}, not the authority — the receipt "
                "contradicts the gate; treat the surface as broken"
            )
        landed.append(result.purchase)
        purchased[item_id] += 1

    return executed_outcome(tuple(landed), user_approved=plan.constraints.user_approved)
