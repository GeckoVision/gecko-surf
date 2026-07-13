"""Day 15 → Day 18: a sharp move becomes an on-chain settlement — the whole chain, $0.

A flagged sharp move is a *signal*; the payout is *settlement*. This bridges the two
examples: the Sharp agent flags a move on a fixture, then the ``txodds_settlement`` agent
(same painful, auth-gated TxLINE API — every call **risk-scored** by the security gateway)
pulls the 3-stage Merkle proof and produces the on-chain ``validate_stat`` settlement
instruction: the exact args a Surfpool profile or a mainnet settle consumes.

    uv run python -m examples.txline_sharp_agent.settlement_sim

Runs **$0 recorded**. The on-chain step lives in ``gecko-programs``, whose Surfpool harness
**profiles** the tx (``FakeSurfpool`` offline $0 / ``RpcSurfpool`` founder-run) and **never
signs or broadcasts** — going to mainnet is always the user's own signed action.
"""

from __future__ import annotations

from typing import Any

from examples.txodds_settlement.agent import (
    SettlementAgent,
    SettlePlan,
    TraderPredicate,
)

from .detector import SharpDetector, SharpMove
from .synthetic import scripted_feed

_RULE = "─" * 68


def settle_from_move(
    move: SharpMove,
    *,
    threshold: int = 2,
    comparison: str = "GreaterThan",
) -> tuple[SettlementAgent, SettlePlan]:
    """Turn a flagged move into a settlement plan for that fixture (risk-scored, $0).

    The market resolves on ``predicate`` (e.g. "score > 2"); the agent watches the score,
    pulls the Merkle proof, and maps it onto the on-chain ``validate_stat`` args.
    """
    agent = SettlementAgent()
    predicate = TraderPredicate(threshold=threshold, comparison=comparison)
    plan = agent.settle(
        fixture_id=move.fixture_id, seq=1, stat_key=1, predicate=predicate
    )
    return agent, plan


def _first_move() -> SharpMove:
    det = SharpDetector(threshold_pct=3.0)
    for snapshot in scripted_feed(move_at=3):
        moves = det.observe(snapshot)
        if moves:
            return moves[0]
    raise RuntimeError("synthetic feed produced no sharp move")


def run() -> int:
    print(
        f"{_RULE}\n  Day 15 → Day 18 · sharp move → trustless on-chain settlement\n{_RULE}"
    )

    move = _first_move()
    print("\n  1 · Sharp agent flags a move (the trading signal):")
    print(f"      {move.summary()}")

    print(
        "\n  2 · Settlement agent settles the market on that fixture (risk-scored, $0):"
    )
    agent, plan = settle_from_move(move, threshold=2, comparison="GreaterThan")
    print(
        f"      {agent.tools} first-call-correct tools over the auth-gated TxLINE API"
    )
    print(f"      security gateway scored {len(agent.audit)} call(s):")
    for g in agent.audit:
        print(f"        · {g.tool} → {g.risk.decision}")

    print(
        "\n  3 · On-chain settlement instruction (validate_stat args from the Merkle proof):"
    )
    print(
        f"      predicate     : {plan.predicate.comparison} {plan.predicate.threshold}"
    )
    print(f"      fixture ts    : {plan.ts}")
    print(f"      stat_a keys   : {sorted(_present(plan.stat_a))}")
    print(f"      has main proof: {plan.main_tree_proof is not None}")

    print(f"\n{_RULE}\n  Where it runs")
    print(f"{_RULE}")
    print("  This plan is serialized into gecko-settlement::settle, which CPIs")
    print(
        "  txoracle::validate_stat — the program never decides the outcome, the proof does."
    )
    print(
        "  gecko-programs profiles the tx on Surfpool (FakeSurfpool $0 offline / RpcSurfpool"
    )
    print(
        "  founder-run) — it PROFILES, never signs or broadcasts. Mainnet is your signed step.\n"
    )
    return 0


def _present(d: dict[str, Any]) -> list[str]:
    return [k for k, v in d.items() if v not in (None, [], "")]


if __name__ == "__main__":
    raise SystemExit(run())
