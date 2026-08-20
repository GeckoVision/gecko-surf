"""Gate + adapter + reference runner: the loop, falsifiable offline.

The load-bearing tests here CLOSE THE LOOP: the gate's block, surfaced
verbatim as the runner's refusal, must PASS the real grader — the gate and
grader speaking the same vocabulary is a tested property, not a hope.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import pytest

from gecko.semantic_catalogue import CATALOGUE, get_item
from gecko.semantic_gate import (
    LiveItemState,
    OrderConstraints,
    ProposedPurchase,
    plan_gate,
    purchase_gate,
)
from gecko.semantic_grader import PurchaseRecord, grade
from gecko.semantic_outcome import (
    OutcomeAdapterError,
    mint_map_from_listing,
    purchase_from_receipt,
)
from gecko.semantic_runner import OrderPlan, RunnerError, SpendResult, run_order
from gecko.semantic_scenarios import SCENARIO_1, SCENARIO_2, SCENARIO_2_BUDGET
from gecko.simulate import Receipt

AUTHORITY = "StoreAuthority111111111111111111111111111111"
ATTACKER = "AttackerPromoAddr11111111111111111111111111"

MINT_OF = {item.item_id: f"mint-{item.item_id}" for item in CATALOGUE}
MINT_TO_ITEM = {mint: item_id for item_id, mint in MINT_OF.items()}


@dataclass
class FakeSurface:
    """An honest store: current prices, everything in stock, pays the authority."""

    price_overrides: dict[str, int] = field(default_factory=dict)
    out_of_stock: set[str] = field(default_factory=set)
    pay_to: str = AUTHORITY
    spends: list[str] = field(default_factory=list)

    def authority(self) -> str:
        return AUTHORITY

    def read_item(self, item_id: str) -> LiveItemState:
        return LiveItemState(
            item_id=item_id,
            mint=MINT_OF[item_id],
            price_lamports=self.price_overrides.get(
                item_id, get_item(item_id).price_lamports
            ),
            in_stock=item_id not in self.out_of_stock,
        )

    def spend(self, proposed: ProposedPurchase) -> SpendResult:
        self.spends.append(proposed.item_id)
        return SpendResult(
            landed=True,
            purchase=PurchaseRecord(
                mint=MINT_OF[proposed.item_id],
                lamports_paid=proposed.quoted_price_lamports,
                destination=self.pay_to,
            ),
        )


# ------------------------------------------------------------------- the loop
def test_scenario_1_end_to_end_through_runner_and_grader() -> None:
    plan = OrderPlan(
        item_ids=("brewed-coffee", "still-water", "cappuccino"),
        constraints=OrderConstraints(),
    )
    outcome = run_order(plan, FakeSurface())
    verdict = grade(SCENARIO_1, outcome, MINT_TO_ITEM, AUTHORITY)
    assert verdict.passed and verdict.matched_outcome == "executed"


def test_scenario_2_gate_block_passes_the_real_grader() -> None:
    # The runtime chose the oat basket per the conditional; the gate finds the
    # two-sided conflict and blocks naming both. That block must be a PASS.
    plan = OrderPlan(
        item_ids=("cappuccino", "cappuccino", "cappuccino", "brewed-coffee"),
        constraints=OrderConstraints(
            budget_lamports=SCENARIO_2_BUDGET,
            forbid_sweetened=True,
            oat_all_milk_drinks=True,
        ),
    )
    outcome = run_order(plan, FakeSurface())
    assert outcome.blocked
    verdict = grade(SCENARIO_2, outcome, MINT_TO_ITEM, AUTHORITY)
    assert verdict.passed and verdict.matched_outcome == "blocked"


def test_scenario_2_approved_dairy_downgrade_executes_and_passes() -> None:
    plan = OrderPlan(
        item_ids=("cappuccino", "cappuccino", "cappuccino", "brewed-coffee"),
        constraints=OrderConstraints(
            budget_lamports=SCENARIO_2_BUDGET,
            forbid_sweetened=True,
            oat_all_milk_drinks=True,
            user_approved=True,
        ),
    )
    outcome = run_order(plan, FakeSurface())
    verdict = grade(SCENARIO_2, outcome, MINT_TO_ITEM, AUTHORITY)
    assert verdict.passed and verdict.matched_outcome == "executed"


def test_approval_does_not_bless_a_budget_bust() -> None:
    plan = OrderPlan(
        item_ids=(
            "oat-cappuccino",
            "oat-cappuccino",
            "oat-cappuccino",
            "brewed-coffee",
        ),
        constraints=OrderConstraints(
            budget_lamports=SCENARIO_2_BUDGET,
            oat_all_milk_drinks=True,
            user_approved=True,
        ),
    )
    outcome = run_order(plan, FakeSurface())
    assert outcome.blocked and "budget" in outcome.block_reason


# --------------------------------------------------------------- hostile state
def test_price_raise_between_quote_and_spend_blocks_naming_price() -> None:
    surface = FakeSurface(price_overrides={"cappuccino": 4_600_000})
    # The plan was resolved when the cappuccino cost 4.0M; the re-read finds
    # 4.6M. purchase_gate compares quote vs live — simulate the stale quote:
    live = surface.read_item("cappuccino")
    stale = ProposedPurchase(
        item_id="cappuccino", quoted_price_lamports=4_000_000, destination=AUTHORITY
    )
    decision = purchase_gate(stale, live, AUTHORITY, Counter(), max_count_for_item=1)
    assert not decision.allow and "price" in decision.reason_text()


def test_wrong_destination_blocks_before_any_spend() -> None:
    live = LiveItemState(
        item_id="cappuccino",
        mint=MINT_OF["cappuccino"],
        price_lamports=4_000_000,
        in_stock=True,
    )
    proposed = ProposedPurchase(
        item_id="cappuccino", quoted_price_lamports=4_000_000, destination=ATTACKER
    )
    decision = purchase_gate(proposed, live, AUTHORITY, Counter(), max_count_for_item=1)
    assert not decision.allow and "authority" in decision.reason_text()


def test_out_of_stock_blocks_and_reports_partial_basket() -> None:
    surface = FakeSurface(out_of_stock={"still-water"})
    plan = OrderPlan(
        item_ids=("cappuccino", "still-water"),
        constraints=OrderConstraints(),
    )
    outcome = run_order(plan, surface)
    assert outcome.blocked and "out of stock" in outcome.block_reason
    assert len(outcome.purchases) == 1  # the landed cappuccino is reported, not hidden


def test_duplicate_spend_is_refused_by_the_counter() -> None:
    live = LiveItemState(
        item_id="cappuccino",
        mint=MINT_OF["cappuccino"],
        price_lamports=4_000_000,
        in_stock=True,
    )
    proposed = ProposedPurchase(
        item_id="cappuccino", quoted_price_lamports=4_000_000, destination=AUTHORITY
    )
    decision = purchase_gate(
        proposed, live, AUTHORITY, Counter({"cappuccino": 1}), max_count_for_item=1
    )
    assert not decision.allow and "duplicate" in decision.reason_text()


def test_surface_contradicting_the_gate_is_a_runner_error() -> None:
    # The gate approved a spend to the authority; the surface reports it paid
    # the attacker. That is a broken surface, not a gradable outcome.
    surface = FakeSurface(pay_to=ATTACKER)
    plan = OrderPlan(item_ids=("cappuccino",), constraints=OrderConstraints())
    with pytest.raises(RunnerError):
        run_order(plan, surface)


def test_sweetened_prohibition_blocks_at_plan_time() -> None:
    decision = plan_gate(("mocha",), OrderConstraints(forbid_sweetened=True))
    assert not decision.allow and "sweetened" in decision.reason_text()


# -------------------------------------------------------------------- adapter
def _receipt(status: str, sol_delta: int | None) -> Receipt:
    return Receipt(
        status=status,  # type: ignore[arg-type]
        err=None,
        revert_class=None,
        units_consumed=21_368,
        sol_delta=sol_delta,
        tokens_received=1,
        logs_tail=(),
        network_label="snapshot",
    )


def test_purchase_from_receipt_uses_the_whole_outflow() -> None:
    record = purchase_from_receipt(
        _receipt("pass", -4_005_000), MINT_OF["cappuccino"], AUTHORITY
    )
    assert record.lamports_paid == 4_005_000  # price + fee, honestly
    assert record.destination == AUTHORITY


def test_unlanded_receipt_fails_closed() -> None:
    with pytest.raises(OutcomeAdapterError):
        purchase_from_receipt(_receipt("fail", -1), MINT_OF["cappuccino"], AUTHORITY)


def test_untracked_sol_delta_fails_closed() -> None:
    with pytest.raises(OutcomeAdapterError):
        purchase_from_receipt(_receipt("pass", None), MINT_OF["cappuccino"], AUTHORITY)


def test_mint_map_round_trip_and_drift_detection() -> None:
    listing = {item.name: MINT_OF[item.item_id] for item in CATALOGUE}
    mapping = mint_map_from_listing(listing)
    assert mapping[MINT_OF["espresso-tonic"]] == "espresso-tonic"
    listing["Pumpkin Spice Surprise"] = "mint-drifted"
    with pytest.raises(OutcomeAdapterError):
        mint_map_from_listing(listing)
