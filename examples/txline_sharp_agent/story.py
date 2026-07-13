"""The whole use case, end to end, in one $0 run — the demo-day story.

A trader wants a Solana agent that watches the World Cup and settles prediction markets.
Starting from nothing, with Gecko as the brain and a policy-gated wallet as the hands:

  0 · User sets up an agentic wallet — funds it, authorizes ONE policy. (3 acts, done.)
  1 · Gecko comprehends the paywalled TxLINE odds API — first-call-correct tools, no docs.
  2 · Agent subscribes to TxLINE — the wallet signs within the policy.
  3 · Agent monitors the feed and flags a sharp move — the trading signal.
  4 · Agent settles the market on TxLINE's Merkle proof — the wallet signs within the policy.

Everything except the 3 wallet acts is the agent's job. Gecko never holds keys or funds.
Runs $0 offline (recorded + a sandbox wallet); mainnet swaps the signer edge only.

    uv run python -m examples.txline_sharp_agent.story
"""

from __future__ import annotations

from pathlib import Path

from gecko import AgentApiClient
from gecko.access import stub_session

from .detector import SharpDetector
from .settlement_sim import settle_from_move
from .synthetic import scripted_feed
from .wallet_sim import Policy, SandboxWallet, TxIntent

SPEC = str(
    Path(__file__).resolve().parents[1] / "txline_demo" / "spec" / "txline_openapi.yaml"
)
_RULE = "─" * 70
_SUBSCRIBE_USDC = 20.0
_SETTLE_FEE_USDC = 1.0


def _step(n: int, title: str) -> None:
    print(f"\n{_RULE}\n  {n} · {title}\n{_RULE}")


def run() -> int:
    print(
        f"{_RULE}\n  Gecko × TxLINE — a Solana trading + settlement agent, end to end ($0)\n{_RULE}"
    )

    # 0 — the user's ENTIRE surface: fund, set up, authorize one policy.
    _step(0, "User sets up the agentic wallet (the only manual steps)")
    wallet = SandboxWallet(funded=100.0)  # fund + set up (sandbox = $0 ephemeral)
    policy = Policy(
        max_spend_usdc=50.0,
        allowed_purposes=frozenset({"txline-subscription", "market-settlement"}),
    )
    wallet.authorize(policy)  # authorize ONE policy
    print(
        f"    funded ${wallet.funded_usdc():g} · policy: ≤ ${policy.max_spend_usdc:g} for"
    )
    print("    {txline-subscription, market-settlement}.  Nothing else is on the user.")

    # 1 — Gecko comprehends the painful API.
    _step(1, "Gecko comprehends the paywalled TxLINE API (no docs, no client)")
    client = AgentApiClient(SPEC, session=stub_session())
    n_tools = len(client.list_tools())
    print(
        f"    {n_tools} first-call-correct tools. The agent never read TxLINE's docs."
    )

    # 2 — subscribe; the wallet signs within policy.
    _step(2, "Agent subscribes to TxLINE — the wallet signs within the policy")
    sub = wallet.sign_within_policy(
        TxIntent("txline-subscription", _SUBSCRIBE_USDC, "subscribe (on-chain, USDC)")
    )
    print(
        f"    ✓ subscribed for ${_SUBSCRIBE_USDC:g}  [{sub.ref}] · ${wallet.funded_usdc():g} left"
    )

    # 3 — monitor the feed; flag the sharp move.
    _step(3, "Agent monitors the odds and flags a sharp move")
    det = SharpDetector(threshold_pct=3.0)
    move = None
    for snapshot in scripted_feed(move_at=3):
        hits = det.observe(snapshot)
        if hits:
            move = hits[0]
            break
    assert move is not None
    print(f"    ⚡ {move.summary()}")

    # 4 — settle the market on the Merkle proof; the wallet signs within policy.
    _step(4, "Agent settles the prediction market — the wallet signs within the policy")
    agent, plan = settle_from_move(move, threshold=2, comparison="GreaterThan")
    blocked = sum(1 for g in agent.audit if g.risk.decision == "block")
    print(
        f"    security gateway scored {len(agent.audit)} TxLINE call(s), {blocked} blocked"
    )
    settle = wallet.sign_within_policy(
        TxIntent("market-settlement", _SETTLE_FEE_USDC, "settle on the Merkle proof")
    )
    print(f"    ✓ settle signed for ${_SETTLE_FEE_USDC:g}  [{settle.ref}]")
    print(
        f"      validate_stat: {plan.predicate.comparison} {plan.predicate.threshold} · main proof ✓"
    )

    # recap
    print(f"\n{_RULE}\n  The whole thing")
    print(f"{_RULE}")
    print(
        "  The user did 3 acts (fund · set up · authorize). The agent did everything else:"
    )
    print(
        "  comprehended a paywalled API, subscribed, caught the move, and settled a market —"
    )
    print(
        "  the wallet signing only within the policy, Gecko never holding a key or a dollar."
    )
    print(
        f"  ${wallet.funded_usdc():g} USDC left, all offline, $0. Mainnet: swap the signer edge only.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
