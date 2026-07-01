"""End-to-end demo (recorded mode, $0): natural goal -> discover -> correct call -> data.

No human reads the docs, no integration code is written. Run:
    uv run python -m gecko.demo
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .client import AgentApiClient
from .sample import example_from_schema

DEFAULT_SPEC = str(
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "txodds_docs.yaml"
)
# The out-of-band trust anchor for the live TxODDS demo. Passed explicitly so (a) the
# customer's JWT+token pin to THIS host — a poisoned spec file's servers[0] can never
# redirect auth to an attacker — and (b) the live call targets prod deliberately, not
# by silently defaulting to the spec's servers[0].
TXODDS_BASE_URL = "https://txline.txodds.com"

GOALS = [
    "get the latest live odds for a football fixture",
    "get the score updates for a match",
    "list the upcoming fixtures",
]


def _fill_required(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    return {
        name: example_from_schema(props[name]) for name in props if name in required
    }


def run(
    spec: str = DEFAULT_SPEC,
    goals: list[str] = GOALS,
    mode: str = "recorded",
    session=None,
) -> list[dict[str, Any]]:
    client = AgentApiClient(spec, session=session)
    steps: list[dict[str, Any]] = []
    for goal in goals:
        hits = client.search(goal)
        if not hits:
            steps.append({"goal": goal, "error": "no endpoint found"})
            continue
        top = hits[0]
        tool = client._tool_by_name[top["name"]]
        result = client.call(top["name"], _fill_required(tool), mode=mode)
        steps.append(
            {
                "goal": goal,
                "discovered": f"{top['method']} {top['path']}",
                "called": result["request"],
                "data_sample": result["data"],
            }
        )
    return steps


def live_demo() -> None:
    """Real TxODDS data via a live session. Auto-used when TXODDS_API_TOKEN is set."""
    import os

    from .access import Session

    import json as _json

    token = os.environ.get("TXODDS_API_TOKEN")
    jwt = os.environ.get("TXODDS_JWT", "")
    sess_path = os.path.expanduser("~/.gecko/txodds-session.json")
    if not token and os.path.exists(sess_path):
        d = _json.load(open(sess_path))
        token, jwt = d.get("api_token"), d.get("jwt", "")
    if not token:
        print("No live session — run scripts/subscribe.py --broadcast first.")
        return
    client = AgentApiClient(
        DEFAULT_SPEC,
        base_url=TXODDS_BASE_URL,
        session=Session(jwt=jwt, api_token=token),
    )
    print("Gecko — LIVE mode (real TxODDS World Cup data)\n" + "=" * 56)

    def tool_for(path: str) -> str:
        return next(
            t["name"] for t in client.list_tools() if t["_invoke"]["path"] == path
        )

    fixtures = client.call(tool_for("/api/fixtures/snapshot"), {}, mode="live")
    rows = fixtures["data"] if isinstance(fixtures["data"], list) else []
    first = rows[0] if rows else {}
    print("\nGOAL: what World Cup matches are coming up?")
    print(
        f"  CALLED: {fixtures['method']} {fixtures['request']}  (HTTP {fixtures['status']})"
    )
    if first:
        print(
            f"  → {first.get('Participant1')} vs {first.get('Participant2')}  (FixtureId {first.get('FixtureId')})"
        )

    chosen = None
    for row in rows[:10]:
        f = row.get("FixtureId")
        if f is None:
            continue
        odds = client.call(
            tool_for("/api/odds/snapshot/{fixtureId}"), {"fixtureId": f}, mode="live"
        )
        if isinstance(odds["data"], list) and odds["data"]:
            chosen = (row, odds)
            break
    if chosen:
        row, odds = chosen
        print("\nGOAL: get live odds for a match with an open market")
        print(
            f"  → {row.get('Participant1')} vs {row.get('Participant2')}  (FixtureId {row.get('FixtureId')})"
        )
        print(f"  CALLED: {odds['method']} {odds['request']}  (HTTP {odds['status']})")
        print(f"  LIVE ODDS: {json.dumps(odds['data'])[:380]}")
    elif first.get("FixtureId") is not None:
        print(
            f"\nGOAL: live odds for {first.get('Participant1')} vs {first.get('Participant2')}"
        )
        print(
            "  scanned 10 fixtures — all HTTP 200 but no open market yet (odds populate near kickoff)."
        )


def main() -> None:
    import os

    if os.environ.get("TXODDS_API_TOKEN") or os.path.exists(
        os.path.expanduser("~/.gecko/txodds-session.json")
    ):
        live_demo()
        return
    print("Gecko — make any API agent-usable (recorded mode, $0)\n" + "=" * 56)
    for step in run():
        print(f"\nGOAL: {step['goal']}")
        if step.get("error"):
            print("  (no matching endpoint)")
            continue
        print(f"  DISCOVERED: {step['discovered']}")
        print(f"  CALLED:     {step['called']}")
        print(f"  DATA:       {json.dumps(step['data_sample'])[:180]}")


if __name__ == "__main__":
    main()
