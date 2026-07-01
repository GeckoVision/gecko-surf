"""Task-based first-call-correct evaluation (generic, API-agnostic).

Given a client and a list of ``{goal, expect_op, args}`` tasks, measure whether the
comprehension layer (a) retrieves the right operation for a natural-language goal
(top-1 / top-5) and (b) builds a well-formed request for it. Recorded/offline;
control-plane (records only outcome metadata — tool, rank, ok/reason — never payloads).

This is the falsifiable scorecard behind the V1 "lift" claim: point it at any API the
agent comprehends, with any task set, and read the numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, get_args

from .client import AgentApiClient

# --- Golden-set retrieval eval (the frozen bar every semantic-catalog stage must beat) ---
#
# Single source of truth for the shared types. Every consumer (tests, scorecards, the
# baseline script) imports these — never redeclares them.

Archetype = Literal[
    "keyword_echo",
    "paraphrase_no_overlap",
    "near_dup_disambiguation",
    "out_of_scope",
]
GOLDEN_ARCHETYPES: frozenset[str] = frozenset(get_args(Archetype))

# The closed set of retrieval-depth cutoffs the scorecard reports. Score at depth >= 20
# so an op at true rank 8 is distinguishable from a total miss (pass limit >= max(RECALL_KS)).
RECALL_KS: tuple[int, ...] = (1, 3, 5, 20)


class GoldenError(ValueError):
    """A golden JSONL file is malformed (bad archetype, missing field, non-list expect_ops)."""


@dataclass(frozen=True)
class GoldenTask:
    """One labeled intent. ``expect_ops`` is a LIST of valid tool names (>=2 for genuinely
    ambiguous intents; ``[]`` for out-of-scope); ``args`` are control-plane-clean
    placeholders only (never payloads/secrets)."""

    goal: str
    expect_ops: tuple[str, ...]
    archetype: str
    args: Mapping[str, Any] = field(default_factory=dict)


def load_golden(path: str | Path) -> list[GoldenTask]:
    """Parse + validate a frozen golden JSONL file into typed tasks."""
    tasks: list[GoldenTask] = []
    for lineno, raw in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldenError(f"{path}:{lineno}: invalid JSON ({exc})") from exc
        if not isinstance(obj, dict) or "goal" not in obj or "expect_ops" not in obj:
            raise GoldenError(f"{path}:{lineno}: missing 'goal'/'expect_ops'")
        if not isinstance(obj["expect_ops"], list):
            raise GoldenError(f"{path}:{lineno}: 'expect_ops' must be a list")
        arch = obj.get("archetype", "")
        if arch not in GOLDEN_ARCHETYPES:
            raise GoldenError(f"{path}:{lineno}: unknown archetype {arch!r}")
        tasks.append(
            GoldenTask(
                goal=obj["goal"],
                expect_ops=tuple(obj["expect_ops"]),
                archetype=arch,
                args=obj.get("args", {}) or {},
            )
        )
    return tasks


def _recall_mrr(ranks: list[int | None]) -> dict[str, Any]:
    """recall@k (rank <= k) and MRR (mean 1/rank, 0 on miss) over positive-task ranks."""
    n = len(ranks) or 1
    return {
        "recall_at": {
            k: sum(1 for r in ranks if r is not None and r <= k) / n for k in RECALL_KS
        },
        "mrr": sum((1.0 / r) if r else 0.0 for r in ranks) / n,
    }


def evaluate_golden(
    client: AgentApiClient, tasks: list[GoldenTask], limit: int = 30
) -> dict[str, Any]:
    """Retrieval scorecard over a golden set — recall@k / MRR, additive to the ranker.

    Positive tasks (``expect_ops`` non-empty): a hit is ``min rank`` over ANY expected op
    (the ambiguous-intent adapter). Out-of-scope tasks (``expect_ops == []``): correct iff
    the top-1 is empty or below the confidence floor (a score-0 fallback), never a
    confident false positive.

    Because the 0/97 fallback is only ever appended when there is NO genuine hit, the
    per-task result also yields the *pre-fix* projection for free: dropping fallback
    candidates reproduces the old ``score > 0`` behaviour exactly. So one scored run emits
    both ``before_fix`` and ``after_fix`` retrieval numbers — the 0/97 fix's measured lift.
    Score at ``limit >= max(RECALL_KS)`` so no true rank is censored below 20.
    """
    per_task: list[dict[str, Any]] = []
    ranks_after: list[int | None] = []
    ranks_before: list[int | None] = []
    oos_pass_before: list[bool] = []
    oos_pass_after: list[bool] = []

    for t in tasks:
        hits = client.search_scored(t.goal, limit=limit)
        if not t.expect_ops:  # out-of-scope
            top1 = hits[0] if hits else None
            genuine_top1 = next((h for h in hits if not h.is_fallback), None)
            pass_before = genuine_top1 is None  # old search returned [] on zero-overlap
            pass_after = top1 is None or top1.is_fallback  # below the confidence floor
            oos_pass_before.append(pass_before)
            oos_pass_after.append(pass_after)
            per_task.append(
                {
                    "goal": t.goal,
                    "expect_ops": [],
                    "archetype": t.archetype,
                    "rank": None,
                    "hit": pass_after,
                    "top1": top1.name if top1 else None,
                    "top1_is_fallback": bool(top1 and top1.is_fallback),
                }
            )
            continue

        expect = set(t.expect_ops)
        matches = [(i + 1, h) for i, h in enumerate(hits) if h.name in expect]
        rank_after = min((p for p, _ in matches), default=None)
        genuine = [p for p, h in matches if not h.is_fallback]
        rank_before = min(genuine) if genuine else None
        ranks_after.append(rank_after)
        ranks_before.append(rank_before)
        per_task.append(
            {
                "goal": t.goal,
                "expect_ops": list(t.expect_ops),
                "archetype": t.archetype,
                "rank": rank_after,
                "rank_before_fix": rank_before,
                "hit": rank_after is not None,
                "via_fallback": rank_after is not None and rank_before is None,
            }
        )

    n_oos = len(oos_pass_after) or 1
    return {
        "n_positive": len(ranks_after),
        "n_oos": len(oos_pass_after),
        "before_fix": _recall_mrr(ranks_before),
        "after_fix": _recall_mrr(ranks_after),
        "oos_pass_rate": {
            "before_fix": sum(oos_pass_before) / n_oos,
            "after_fix": sum(oos_pass_after) / n_oos,
        },
        "per_task": per_task,
    }


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
