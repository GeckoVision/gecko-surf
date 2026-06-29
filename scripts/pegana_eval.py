"""Pegana first-call-correct scorecard (recorded, $0) — the second painful API.

Ingests the committed Pegana OpenAPI surface unilaterally (it is NOT in any catalog),
comprehends it with a PUBLIC (no-auth) session, and scores first-call-correctness on
representative tasks — including the real gotchas from Gecko's hand-integration. Thin
CLI: the logic lives in ``gecko.evaluate`` and the engine; this file is transport.

    uv run python scripts/pegana_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the package importable when run as a plain script (python scripts/pegana_eval.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.access import public_session  # noqa: E402
from gecko.caller import CallError  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import evaluate_tasks  # noqa: E402

SPEC = str(
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "pegana_openapi.json"
)
# A real SPL mint (jitoSOL) — the agent holds a mint at decision time, not a symbol.
JITO_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"

TASKS = [
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


def main() -> None:
    client = AgentApiClient(SPEC, session=public_session())
    surfaced = client.list_tools()
    hidden = len(client.operations) - len(surfaced)
    print("Gecko — Pegana first-call-correct scorecard (recorded, $0)")
    print("=" * 64)
    print(
        f"ingested {len(client.operations)} ops · surfaced to agent (public) {len(surfaced)} · "
        f"auth-gated hidden {hidden}"
    )

    card = evaluate_tasks(client, TASKS)
    for r in card["results"]:
        mark = "OK" if r["top1"] else ("~5" if r["in_top5"] else "XX")
        print(f"\n[{mark}] {r['goal']}")
        print(
            f"     expect={r['expect']}  picked={r['picked']}  rank={r['rank']}  well_formed={r['well_formed']}"
        )
        if r["reason"]:
            print(f"     reason: {r['reason']}")

    print("\n" + "-" * 64)
    print(
        f"top-1 {card['top1_rate']:.0%} · top-5 {card['top5_rate']:.0%} · well-formed {card['well_formed_rate']:.0%}"
    )

    # Gotcha — the auth boundary: a telegram_jwt op is hidden AND refused if forced.
    print(
        "\nauth boundary (telegram_jwt): an agent must never fire these on a public read"
    )
    try:
        client.prepare("list_subs", {})
        print("  list_subs: prepared — UNEXPECTED (the guard failed)")
    except CallError as exc:
        print(f"  list_subs: correctly refused — {exc}")

    # Outcome log (metadata only) -> private/ (gitignored). Control-plane: no payloads.
    log = Path(__file__).resolve().parent.parent / "private" / "pegana_scorecard.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        for r in card["results"]:
            fh.write(json.dumps(r) + "\n")
    print(f"\nwrote {log}")


if __name__ == "__main__":
    main()
