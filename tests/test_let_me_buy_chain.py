"""The lifecycle chain in the graph — the first multi-instruction chain on a program
surface, and the fail-closed verdict that decides whether it may be called ordered.

What an IDL states is per-instruction: these accounts, these args. What it never states
is that ONE call must follow ANOTHER — that `mark_as_delivered` settles a receipt only a
landed `make_purchase` wrote, and that both address the same store account. That join is
this module's subject, and these tests hold it to four rules:

1. The chain rail is the ACCOUNT rail one level up, never a bypass. Every step still
   goes through ``_derive_plan`` → ``derivation_order_with_cycle``; the instructions go
   through ``chain_order_with_cycle``, the same ``(order, cycle)`` contract. An
   unorderable chain is REPORTED with every step and its full derive plan — never an
   empty tuple, never a raise.
2. The verdict is TRI-STATE and fails closed. ``ordered`` requires an explicit ``AGREE``
   on the chain-agreement axis (T3's comparison against landed transactions). Absent,
   misspelled, or non-string evidence is ``not_evaluated``; ``DISAGREE`` is
   ``unresolved``. Neither is reachable as a zero value — ``ChainPlan()`` raises.
3. The link is COMPUTED, not declared. A declared edge becomes a ``ChainLink`` only if
   the shared account is actually present, and unflagged, on BOTH endpoints; the link's
   provenance is the weaker of the two ends.
4. Every edge answers the four questions on its own: what it connects, over which
   account, on what basis, and what evidence would refute it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, get_args

import pytest

# T3's evidence set: the comparison of the derived `make_purchase` account list against
# the accounts twelve landed mainnet transactions actually used. Imported rather than
# re-derived so this file consumes the verdict instead of minting its own.
from test_let_me_buy_chain_agreement import (
    CHAIN_VERDICTS as AGREEMENT_VERDICTS,
    MAKE_PURCHASE_ACCOUNT_ORDER,
    STORE_NAME,
    _let_me_buy,
    _load_records,
    chain_verdict,
)

from gecko.find_start import (
    CHAIN_VERDICTS,
    ChainPlan,
    _chain_plan,
    _wired_cards,
    declared_chains,
    find_start,
    format_result,
)
from gecko.pda import PdaNode, VariablePdaSeedNode, derive_pda
from gecko.plan_refusals import DISTINCT_ACCOUNT_RULES
from gecko.program_graph import chain_order_with_cycle, derivation_order_with_cycle
from gecko.provenance import ChainStatus

CHAIN = "sell_and_deliver"
STORE_CHAIN = "open_a_store"


def _cards() -> list[Any]:
    return _wired_cards()


def _card(instruction: str) -> Any:
    return next(
        c for c in _cards() if c.api_id == "let_me_buy" and c.instruction == instruction
    )


def _plan_for(instruction: str, verdicts: dict[str, Any] | None = None) -> ChainPlan:
    cards = _cards()
    card = next(
        c for c in cards if c.api_id == "let_me_buy" and c.instruction == instruction
    )
    return _chain_plan(card, cards, verdicts)


def _landed_verdict() -> str:
    """The chain-agreement verdict, computed from T3's recorded fixture. No network."""
    return chain_verdict(_let_me_buy(), _load_records())


# --------------------------------------------------------------------------------------
# the closed status vocabulary (S1's type spec)
# --------------------------------------------------------------------------------------


def test_the_chain_status_vocabulary_is_closed_and_carries_not_evaluated() -> None:
    """``==`` on purpose: a fourth member is a visible, failing edit, and the
    not-evaluated member must exist at all — a verdict type with no representation for
    "we did not check" makes that case fall into the type's zero value."""
    assert get_args(ChainStatus) == ("ordered", "unresolved", "not_evaluated")


def test_a_chain_plan_cannot_be_default_constructed() -> None:
    """No field has a default, so ``not_evaluated`` is never the zero value: a plan that
    nobody filled in cannot read as a plan that was checked."""
    with pytest.raises(TypeError):
        ChainPlan()  # type: ignore[call-arg]


def test_the_accepted_verdict_vocabulary_matches_the_producer() -> None:
    """The set find_start accepts and the set the comparison produces are pinned to each
    other. If they drift, a real verdict would silently normalize to NOT_EVALUATED and
    every chain would quietly stop being orderable — a failure that hides as caution."""
    assert (
        CHAIN_VERDICTS == AGREEMENT_VERDICTS == {"AGREE", "DISAGREE", "NOT_EVALUATED"}
    )


# --------------------------------------------------------------------------------------
# C1 — the verdict is tri-state and fails closed
# --------------------------------------------------------------------------------------


def test_a_make_purchase_plan_with_no_chain_verdict_is_flagged() -> None:
    """THE FAIL-CLOSED TEST. Nothing supplies the chain-agreement evidence by default —
    it deliberately does not live in the packaged config — so the honest answer is "not
    evaluated", and the plan must NOT be presented as an ordered chain."""
    plan = _plan_for("make_purchase")
    assert plan.status == "not_evaluated"
    assert plan.verdict == "NOT_EVALUATED"
    assert plan.status != "ordered"
    assert "never checked against landed transactions" in plan.note
    # flagged, not empty: the steps and the link are still reported
    assert [s.instruction for s in plan.steps] == ["make_purchase", "mark_as_delivered"]
    assert len(plan.links) == 1


def test_a_disagreeing_chain_verdict_flags_the_make_purchase_plan() -> None:
    """A chain the landed transactions CONTRADICT is unresolved — never a clean plan
    over an account set that has been refuted."""
    plan = _plan_for("make_purchase", {CHAIN: "DISAGREE"})
    assert plan.status == "unresolved"
    assert plan.verdict == "DISAGREE"
    assert "contradict" in plan.note
    assert [s.instruction for s in plan.steps] == ["make_purchase", "mark_as_delivered"]


@pytest.mark.parametrize(
    "supplied",
    [
        None,
        {},
        {CHAIN: None},
        {CHAIN: ""},
        {CHAIN: "agree"},  # case matters — this is not the closed member
        {CHAIN: "AGREED"},
        {CHAIN: True},  # a bool must never carry chain state
        {CHAIN: 1},
        {CHAIN: ["AGREE"]},
        {"another_chain": "AGREE"},  # a verdict for a DIFFERENT chain proves nothing
    ],
)
def test_evidence_that_cannot_be_read_is_not_evaluated_never_ordered(
    supplied: dict[str, Any] | None,
) -> None:
    """Untrusted input never argues its way into confidence, and a malformed verdict
    never becomes a refusal that did not happen: it degrades to not_evaluated, and the
    call does not raise."""
    plan = _plan_for("make_purchase", supplied)
    assert plan.status == "not_evaluated"
    assert plan.verdict == "NOT_EVALUATED"


def test_the_agree_verdict_from_the_landed_transactions_orders_the_chain() -> None:
    """The one path to ``ordered``: an explicit AGREE, computed from the twelve landed
    transactions T3 recorded. Offline — the fixture is read, nothing is fetched."""
    verdict = _landed_verdict()
    assert verdict == "AGREE"  # if this flips, the chain below must stop being ordered
    plan = _plan_for("make_purchase", {CHAIN: verdict})
    assert plan.status == "ordered"
    assert plan.verdict == "AGREE"
    assert plan.unresolved == ()
    assert "not authorization" in plan.note  # ordered is not permission to send


def test_the_open_a_store_chain_is_not_evaluated_without_its_own_evidence() -> None:
    """The AGREE covers make_purchase's account set only. The store-opening chain was
    never compared against chain, so it stays not_evaluated even when the OTHER chain
    holds an AGREE — evidence does not spread across chains."""
    plan = _plan_for("initialize", {CHAIN: "AGREE"})
    assert plan.name == STORE_CHAIN
    assert plan.status == "not_evaluated"
    assert [s.instruction for s in plan.steps] == ["initialize", "add_product"]


# --------------------------------------------------------------------------------------
# the rail: extended at the chain level, unchanged at the account level
# --------------------------------------------------------------------------------------


def test_unresolvable_chain_returns_flagged_plan_not_empty() -> None:
    """THE ACCEPTANCE TEST. Make the shared ``receipts`` account self-seeded, so
    ``derivation_order_with_cycle`` returns a non-empty cycle for BOTH instructions of
    the purchase chain. The plan must report the whole chain — every step, every derive
    plan, one gap per offender — and must not return an empty tuple, None, or raise."""
    cyclic = PdaNode(
        "receipts",
        (VariablePdaSeedNode("receipts", "account", "pubkey"),),
        "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya",
    )
    cards = [
        replace(c, pdas={**c.pdas, "receipts": cyclic})
        if c.api_id == "let_me_buy"
        else c
        for c in _cards()
    ]
    card = next(c for c in cards if c.instruction == "make_purchase")

    plan = _chain_plan(card, cards, {CHAIN: "AGREE"})  # even WITH an AGREE

    assert plan.status == "unresolved"
    assert plan.steps != ()
    assert [s.instruction for s in plan.steps] == ["make_purchase", "mark_as_delivered"]
    # every step keeps its FULL derive plan, flagged accounts included
    assert len(plan.steps[0].derive_plan) == len(MAKE_PURCHASE_ACCOUNT_ORDER)
    assert plan.steps[1].derive_plan  # the two-account step is not trimmed away
    for step in plan.steps:
        flagged = [s for s in step.derive_plan if s.account == "receipts"]
        assert flagged and flagged[0].provenance == "flagged"
    assert [g.name for g in plan.unresolved] == ["receipts"]
    assert "cannot be carried" in plan.unresolved[0].note
    # the link is not claimed over a flagged account
    assert plan.links == ()


def test_the_chain_order_rail_reports_a_cycle_instead_of_sequencing_it() -> None:
    """The chain-level extension of ``derivation_order_with_cycle``: same contract —
    nothing dropped, the unorderable names returned so the caller flags them."""
    order, cycle = chain_order_with_cycle(
        ["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "b")]
    )
    assert set(order) == {"a", "b", "c"}  # never dropped
    assert cycle == frozenset({"b", "c"})
    assert order.index("a") == 0


def test_extending_the_rail_did_not_relax_the_account_level() -> None:
    """C6. The account-level cyclic branch still FLAGS rather than drops or silently
    orders — the chain level is an extension, not a replacement."""
    from gecko.find_start import _account_step

    pdas = {
        "a": PdaNode("a", (VariablePdaSeedNode("b", "account", "pubkey"),), None),
        "b": PdaNode("b", (VariablePdaSeedNode("a", "account", "pubkey"),), None),
    }
    ordered, cyclic = derivation_order_with_cycle(pdas, ("a", "b"))
    assert set(ordered) == {"a", "b"}
    assert cyclic == frozenset({"a", "b"})
    step = _account_step(
        "a",
        pdas["a"],
        recovered={"a": "recovered from source"},
        overlay_pdas=frozenset(),
        overlay_why={},
        cyclic=cyclic,
    )
    assert step.provenance == "flagged"
    assert "cycle" in step.note


def test_a_start_with_no_declared_chain_says_so_instead_of_claiming_one() -> None:
    """``steps == ()`` means exactly one thing: no chain is declared here. It is
    distinguishable from an unresolvable chain, which always carries its steps."""
    plan = find_start("buy this token on pump and hold it").starts[0].chain
    assert plan.status == "not_evaluated"
    assert plan.steps == ()
    assert plan.name == ""
    assert "no lifecycle chain is DECLARED" in plan.note


# --------------------------------------------------------------------------------------
# the link: the shared receipts PDA is the dependency, and it is computed
# --------------------------------------------------------------------------------------


def test_the_shared_receipts_pda_is_the_link_between_the_steps() -> None:
    plan = _plan_for("make_purchase", {CHAIN: _landed_verdict()})
    (link,) = plan.links
    assert (link.produces, link.consumes) == ("make_purchase", "mark_as_delivered")
    assert link.account == "receipts"
    assert link.kind == "produces"  # a REAL data dependency, not mere ordering
    # the link is only as strong as its weakest end. D1 lifted `receipts` to
    # `extracted` (its pda block is the program's own on-chain IDL's word, and the same
    # IDL lists it as account 0 of BOTH instructions), so the link rises with it.
    assert link.provenance == "extracted"
    # and it really is present on both steps, at the head of each derive plan
    for step in plan.steps:
        assert step.derive_plan[0].account == "receipts"


def test_both_steps_address_the_same_store_account() -> None:
    """The claim under the edge, checked by DERIVATION rather than by assertion: the
    receipts recipe both instructions carry produces one address for one store name."""
    program = _let_me_buy()
    address = derive_pda(program.pdas["receipts"], {"store_name": STORE_NAME}).address
    plan = _plan_for("make_purchase", {CHAIN: _landed_verdict()})
    for step in plan.steps:
        card = _card(step.instruction)
        assert (
            derive_pda(card.pdas["receipts"], {"store_name": STORE_NAME}).address
            == address
        )


def test_the_open_a_store_edge_is_an_initializes_edge_not_a_data_dependency() -> None:
    """The two flows are not the same shape, and the graph says which is which: a store
    somebody else opened satisfies `initializes`; nothing but a landed purchase
    satisfies `produces`."""
    (edge,) = declared_chains("let_me_buy")[0].edges
    assert (edge.produces, edge.consumes) == ("initialize", "add_product")
    assert edge.kind == "initializes"
    (purchase_edge,) = declared_chains("let_me_buy")[1].edges
    assert purchase_edge.kind == "produces"
    assert "receipt_id" in purchase_edge.basis


def test_every_edge_states_its_basis_and_what_would_refute_it() -> None:
    """The done-bar for an edge: what it connects, over what, on what basis, and what
    evidence would refute it — answerable without reading the code."""
    for chain in declared_chains("let_me_buy"):
        for edge in chain.edges:
            assert edge.produces and edge.consumes and edge.account
            assert len(edge.basis) > 40
            assert len(edge.refuted_by) > 40
            assert {edge.produces, edge.consumes} <= {
                s.instruction for s in chain.steps
            }


def test_a_declared_link_over_an_absent_account_is_a_gap_not_a_link() -> None:
    """The link is COMPUTED from the account sets, so a declaration that has drifted
    from the config becomes an honest gap instead of a confident edge."""
    cards = _cards()
    card = next(c for c in cards if c.instruction == "make_purchase")
    stripped = [
        replace(c, accounts=tuple(a for a in c.accounts if a != "receipts"))
        if c.instruction == "mark_as_delivered"
        else c
        for c in cards
    ]
    plan = _chain_plan(card, stripped, {CHAIN: "AGREE"})
    assert plan.status == "unresolved"
    assert plan.links == ()
    assert "does not list" in plan.unresolved[0].note


# --------------------------------------------------------------------------------------
# derivation order, end to end through the router
# --------------------------------------------------------------------------------------


def test_the_chain_steps_come_back_in_derivation_order() -> None:
    for name, expected in (
        (CHAIN, ["make_purchase", "mark_as_delivered"]),
        (STORE_CHAIN, ["initialize", "add_product"]),
    ):
        plan = _plan_for(expected[0])
        assert plan.name == name
        assert [s.instruction for s in plan.steps] == expected
        assert [s.position for s in plan.steps] == [1, 2]


def test_the_make_purchase_step_derives_the_landed_account_order() -> None:
    """The chain's own derive plan is the one the mainnet comparison verified — same
    accounts, same order, nine of nine."""
    plan = _plan_for("make_purchase", {CHAIN: _landed_verdict()})
    assert [s.account for s in plan.steps[0].derive_plan] == list(
        MAKE_PURCHASE_ACCOUNT_ORDER
    )


def test_the_same_token_account_rule_sits_on_the_step_that_carries_both_accounts() -> (
    None
):
    """The plan-time refusal and the chain must agree about WHERE the trap can happen.

    The distinctness rule is keyed on (let_me_buy, make_purchase). If that step's derive
    plan ever stopped carrying both token accounts — renamed, split, dropped — the rule
    would still exist and still read as a control while never being reachable. This ties
    the two together so divergence is a failing test, not a silent no-op.
    """
    rule = DISTINCT_ACCOUNT_RULES[("let_me_buy", "make_purchase")]
    plan = _plan_for("make_purchase", {CHAIN: _landed_verdict()})
    derived = {s.account for s in plan.steps[0].derive_plan}
    assert set(rule.accounts) <= derived
    # ...and the settling step carries neither, which is why the rule is keyed on the
    # instruction and not on the api_id alone.
    settle = {s.account for s in plan.steps[1].derive_plan}
    assert not set(rule.accounts) & settle


def test_bar_shaped_intents_route_to_let_me_buy_instructions() -> None:
    for intent, instruction in (
        ("buy a beer", "make_purchase"),
        ("open a store and sell peanuts", "initialize"),
    ):
        top = find_start(intent).starts[0]
        assert top.kind == "start"
        assert (top.program, top.instruction) == ("let_me_buy", instruction)
        assert top.execute is not None
        assert top.execute["url"].endswith(f"/instructions/{instruction}/build")
        # the chain rides along with the start, naming the call that follows it
        assert top.chain.name
        assert len(top.chain.steps) == 2


def test_the_chain_renders_with_its_status_and_its_link() -> None:
    """A status nothing prints is a gate with no caller."""
    text = format_result(find_start("buy a beer", chain_verdicts={CHAIN: "AGREE"}))
    assert "lifecycle chain: sell_and_deliver [ORDERED] (agreement: AGREE)" in text
    assert (
        "link: receipts [extracted] make_purchase --produces--> mark_as_delivered"
        in text
    )
    assert "refuted by:" in text

    unchecked = format_result(find_start("buy a beer"))
    assert "[NOT EVALUATED] (agreement: NOT_EVALUATED)" in unchecked


def test_the_chain_survives_json_serialization() -> None:
    payload = json.loads(
        json.dumps(find_start("buy a beer", chain_verdicts={CHAIN: "AGREE"}).to_json())
    )
    chain = payload["starts"][0]["chain"]
    assert chain["status"] == "ordered"
    assert [s["instruction"] for s in chain["steps"]] == [
        "make_purchase",
        "mark_as_delivered",
    ]
    assert chain["links"][0]["account"] == "receipts"
