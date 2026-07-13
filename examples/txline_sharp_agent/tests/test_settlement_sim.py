"""The day-15 → day-18 bridge: a flagged move yields a risk-scored settlement plan, $0."""

from __future__ import annotations

from examples.txline_sharp_agent.detector import SharpMove
from examples.txline_sharp_agent.settlement_sim import run, settle_from_move

_MOVE = SharpMove(
    fixture_id=42,
    bookmaker="Pinnacle",
    market="1x2|",
    outcome="Home",
    old_pct=45.6,
    new_pct=54.8,
    delta=9.2,
    ts=1000,
)


def test_settle_from_move_produces_a_plan_and_runs_the_security_gateway():
    agent, plan = settle_from_move(_MOVE, threshold=2, comparison="GreaterThan")
    # the settlement plan carries the on-chain validate_stat args
    assert plan.predicate.comparison == "GreaterThan" and plan.predicate.threshold == 2
    assert "stat_to_prove" in plan.stat_a
    # every TxLINE call was risk-scored (the security gateway), and none blocked here
    assert len(agent.audit) == 2
    assert all(g.risk.decision == "allow" for g in agent.audit)


def test_run_smoke_returns_zero():
    assert run() == 0
