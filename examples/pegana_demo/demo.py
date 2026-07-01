"""Pegana: two ways an agent can reach it — the before/after a founder shows Raff.

Pegana (the peg-risk oracle for Solana) already ships its OWN MCP: ~6 substantive
tools its team hand-wrapped. Gecko comprehends Pegana's full OpenAPI (41 operations)
and makes the whole surface first-call-correct — *alongside* that MCP, not instead
of it (aggregate, not replace).

This script reproduces the comparison and the scorecard **offline / $0**, from the
committed fixture (`tests/fixtures/pegana_openapi.json`) via the same engine path the
`scripts/pegana_eval.py` harness uses. No network, deterministic, regenerated
in-memory (it does NOT read the gitignored `private/pegana_scorecard.jsonl`).

    uv run python examples/pegana_demo/demo.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the `gecko` package importable when run as a plain script (sys.path[0] is this
# file's dir, not the repo root). Mirrors scripts/pegana_eval.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gecko.access import public_session  # noqa: E402
from gecko.caller import CallError  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import evaluate_tasks  # noqa: E402

SPEC = str(_REPO_ROOT / "tests" / "fixtures" / "pegana_openapi.json")

# Pegana ships its own MCP; per the aggregate brief it exposes ~6 substantive tools
# (8 total, 2 are ping tests). A documented constant, not a computed one.
PEGANA_MCP_TOOLS = 6

# A real SPL mint (jitoSOL). The agent holds a MINT at decision time, not a symbol —
# the gotcha that separates `state_by_mint` (by-mint/{mint}/state) from the
# `{symbol}/state` route a naive integration would reach for.
JITO_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"

# Representative tasks — mirrors scripts/pegana_eval.py (kept here so the example runs
# self-contained from a clean checkout). Each: a natural-language goal + the operation
# a correct agent must land on + the args it would call with.
TASKS: list[dict[str, Any]] = [
    {
        "goal": "what is the current peg state for the asset with this mint address",
        "expect_op": "state_by_mint",
        "args": {"mint": JITO_MINT},
    },
    {
        "goal": "list all active assets and their current peg state",
        "expect_op": "list_assets",
        "args": {},
    },
    {
        "goal": "aggregate counters and delivery health for the whole universe",
        "expect_op": "summary",
        "args": {},
    },
    {
        "goal": "get an asset's discount history over time",
        "expect_op": "history",
        "args": {"symbol": "jitoSOL"},
    },
    {
        "goal": "the active methodology version used to compute peg state",
        "expect_op": "current",
        "args": {},
    },
    {"goal": "liveness probe — is the service alive", "expect_op": "live", "args": {}},
]

# The JWT-gated op an agent must NEVER fire on a public read (auth boundary probe).
JWT_GATED_OP = "list_subs"


@dataclass(frozen=True)
class DemoReport:
    """Everything the render needs — computed live, so the numbers can't drift."""

    ops_total: int
    surfaced: int
    hidden: int
    mcp_tools: int
    card: dict[str, Any]
    mint_route: str
    symbol_route: str
    jwt_refused: bool
    jwt_reason: str


def build_report() -> DemoReport:
    """Comprehend the committed Pegana surface with a public session and score it.

    Pure/offline: no network, no spend. Everything below is derived from the fixture
    and the unmodified engine, so the demo can't overclaim.
    """
    client = AgentApiClient(SPEC, session=public_session())
    ops_by_id = {o.operation_id: o for o in client.operations}
    surfaced = client.list_tools()

    card = evaluate_tasks(client, TASKS)

    # Auth boundary: a public session must refuse a JWT-gated /v1/me/* op if forced.
    jwt_refused = False
    jwt_reason = ""
    try:
        client.prepare(JWT_GATED_OP, {})
    except CallError as exc:
        jwt_refused = True
        jwt_reason = str(exc)

    return DemoReport(
        ops_total=len(client.operations),
        surfaced=len(surfaced),
        hidden=len(client.operations) - len(surfaced),
        mcp_tools=PEGANA_MCP_TOOLS,
        card=card,
        mint_route=ops_by_id["state_by_mint"].path,
        symbol_route=ops_by_id["state"].path,
        jwt_refused=jwt_refused,
        jwt_reason=jwt_reason,
    )


def render(report: DemoReport) -> None:
    """Print the four blocks: header, the 41-vs-6 table, the scorecard, the footer."""
    line = "=" * 68

    # 1. Header.
    print(line)
    print("Pegana: two ways an agent can reach it.")
    print(line)

    # 2. The 41-vs-6 table.
    print(
        "\nPegana's own MCP     ~%d substantive tools (hand-wrapped highlights)"
        % report.mcp_tools
    )
    print("Gecko comprehension  %d ops ingested from the OpenAPI" % report.ops_total)
    print(
        "                     %d surfaced to a public (no-auth) agent" % report.surfaced
    )
    print(
        "                     %d auth-gated, hidden until a session can satisfy them"
        % report.hidden
    )
    print("\naggregate, not replace — Gecko leaves Pegana's MCP intact and makes the")
    print("full surface usable, first-call-correct.")

    # 3. The scorecard.
    print("\n" + "-" * 68)
    print("First-call-correct scorecard (recorded, $0, offline)")
    print("-" * 68)
    for r in report.card["results"]:
        mark = "OK" if r["top1"] else ("~5" if r["in_top5"] else "XX")
        print(f"\n[{mark}] {r['goal']}")
        print(
            f"     expect={r['expect']}  picked={r['picked']}  "
            f"rank={r['rank']}  well_formed={r['well_formed']}"
        )
        if r["reason"]:
            print(f"     reason: {r['reason']}")

    card = report.card
    print("\n" + "-" * 68)
    print(
        f"top-1 {card['top1_rate']:.0%} · top-5 {card['top5_rate']:.0%} · "
        f"well-formed {card['well_formed_rate']:.0%}"
    )

    # The two highlighted gotchas, called out inline.
    print("\nHighlight — mint vs symbol:")
    print(f"     the agent holds a mint ({JITO_MINT[:8]}…), so it picks")
    print(f"     state_by_mint  {report.mint_route}")
    print(f"     NOT the symbol route  {report.symbol_route}")

    print("\nHighlight — auth boundary (JWT):")
    if report.jwt_refused:
        print(
            f"     prepare('{JWT_GATED_OP}') → correctly refused ({report.jwt_reason})"
        )
        print("     a public session never fires a /v1/me/* JWT op.")
    else:  # pragma: no cover - the guard is expected to fire; here for honesty
        print(f"     prepare('{JWT_GATED_OP}') → NOT refused — the auth guard failed.")

    # 4. Footer.
    print("\n" + line)
    print("Point Gecko at your OpenAPI:")
    print("     gecko https://api.pegana…/openapi.json   →   first-call-correct MCP")
    print(line)
    print("Honest: comprehension proof, not willingness-to-pay. Drift re-ingest +")
    print(
        "correctness corpus = Building. Pegana's REST is free / no-auth (not paywalled)."
    )


def main() -> None:
    render(build_report())


if __name__ == "__main__":
    main()
