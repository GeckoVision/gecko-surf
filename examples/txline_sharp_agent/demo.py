"""$0 recorded showcase — Gecko + TxLINE Sharp Movement Detector.

    uv run python -m examples.txline_sharp_agent.demo

No keys, no subscription, no network. It:
  1. points Gecko at the paywalled TxLINE odds API and comprehends it (18 first-call-
     correct tools; the two-token auth is satisfied by a stub session for recorded mode),
  2. makes ONE odds call in recorded mode — proving the call is well-formed offline ($0),
  3. replays a synthetic live market (deterministic, schema-valid) through the detector
     and flags the sharp move — the signal a trading agent or a prediction market acts on.

For the reasoning agent (a real Claude tool-use loop over these tools), see ``agent.py``.
"""

from __future__ import annotations

from pathlib import Path

from gecko import AgentApiClient
from gecko.access import stub_session

from .detector import SharpDetector
from .surfcall_tools import ODDS_READS
from .synthetic import scripted_feed

SPEC = str(
    Path(__file__).resolve().parents[1] / "txline_demo" / "spec" / "txline_openapi.yaml"
)

_RULE = "─" * 68


def _header(title: str) -> None:
    print(f"\n{_RULE}\n  {title}\n{_RULE}")


def run() -> int:
    _header("1 · Gecko comprehends the paywalled TxLINE odds API")
    client = AgentApiClient(SPEC, session=stub_session())
    tools = client.list_tools()
    odds = sorted(t["name"] for t in tools if t["name"] in ODDS_READS)
    print(f"  comprehended {len(tools)} operations → first-call-correct tools")
    print(f"  the agent's allow-listed odds reads: {', '.join(odds)}")
    print("  (auth-gated tools are hidden until the session can satisfy them —")
    print("   here a $0 stub session unlocks them for recorded mode)")

    _header("2 · One odds call, recorded mode — proven correct offline, $0")
    fixture_id = 42
    result = client.call(
        "getApiOddsSnapshotFixtureid", {"fixtureId": fixture_id}, mode="recorded"
    )
    print(f"  {result['method']} {result['request']}")
    print(f"  status={result['status']}  mode={result['mode']}")
    print("  → the request is well-formed and the tool was chosen correctly,")
    print("    without a key, a subscription, or a single live call.")

    _header("3 · Replay a synthetic live market → flag the sharp move")
    detector = SharpDetector(threshold_pct=3.0)
    feed = scripted_feed(fixture_id=fixture_id, move_at=3)
    flagged = 0
    for tick, snapshot in enumerate(feed):
        moves = detector.observe(snapshot)
        book = snapshot[0]
        pct = dict(zip(book["PriceNames"], book["Pct"]))
        label = "  ".join(f"{k} {v}%" for k, v in pct.items())
        marker = "⚡ SHARP" if moves else "       "
        print(f"  t{tick}  {label}   {marker}")
        for m in moves:
            flagged += 1
            print(f"        └─ {m.summary()}")

    _header("What an agent does next")
    if flagged:
        print(f"  {flagged} sharp move(s) flagged. A trading agent logs the signal and")
        print(
            "  can act — quote a price, hedge, or (see ../txodds_settlement) settle a"
        )
        print(
            "  prediction market on-chain against TxLINE's Merkle proof, trustlessly."
        )
    else:
        print("  No sharp move this run.")
    print(f"\n  Wire it into your agent for real:  gecko add {SPEC}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
