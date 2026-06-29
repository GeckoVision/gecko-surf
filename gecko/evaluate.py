"""Task-based first-call-correct evaluation (generic, API-agnostic).

Given a client and a list of ``{goal, expect_op, args}`` tasks, measure whether the
comprehension layer (a) retrieves the right operation for a natural-language goal
(top-1 / top-5) and (b) builds a well-formed request for it. Recorded/offline;
control-plane (records only outcome metadata — tool, rank, ok/reason — never payloads).

This is the falsifiable scorecard behind the V1 "lift" claim: point it at any API the
agent comprehends, with any task set, and read the numbers.
"""

from __future__ import annotations

from typing import Any

from .client import AgentApiClient


def evaluate_tasks(
    client: AgentApiClient, tasks: list[dict[str, Any]], limit: int = 5
) -> dict[str, Any]:
    """Run ``tasks`` through search + request-build; return a scorecard.

    Each task: ``{"goal": str, "expect_op": str, "args": dict}``. Retrieval is scored
    against the *surfaced* tools (auth-gated ops a no-auth session can't satisfy are
    already hidden), and well-formedness is checked by preparing the EXPECTED op so the
    request-builder is measured independently of retrieval.
    """
    results: list[dict[str, Any]] = []
    for task in tasks:
        goal = task["goal"]
        expect = task["expect_op"]
        args = task.get("args", {})
        names = [h["name"] for h in client.search(goal, limit=limit)]
        rank = names.index(expect) + 1 if expect in names else None
        well_formed = True
        reason = ""
        try:
            client.prepare(expect, args)
        except Exception as exc:  # noqa: BLE001 - any failure is "not well-formed", recorded
            well_formed = False
            reason = f"{type(exc).__name__}: {exc}"
        results.append(
            {
                "goal": goal,
                "expect": expect,
                "picked": names[0] if names else None,
                "rank": rank,
                "top1": bool(names) and names[0] == expect,
                "in_top5": rank is not None,
                "well_formed": well_formed,
                "reason": reason,
            }
        )
    n = len(results) or 1
    return {
        "results": results,
        "top1_rate": sum(r["top1"] for r in results) / n,
        "top5_rate": sum(r["in_top5"] for r in results) / n,
        "well_formed_rate": sum(r["well_formed"] for r in results) / n,
    }
