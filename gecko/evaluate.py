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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, get_args

from .client import AgentApiClient
from .ingest import Operation

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


class AmbiguousMetric(KeyError):
    """A retired card key that does not say WHICH retrieval reading it means.

    Raised instead of answering, because both readings are real numbers and the wrong one
    has already shipped as a headline (see ``evaluate_golden``).
    """

    def __str__(self) -> str:  # KeyError's repr quotes the message; this reads plainly
        return str(self.args[0]) if self.args else ""


_READING_HELP = (
    "{k!r} is retired: it does not say which reading it is. Use {ranker!r} (genuine hits "
    "only — the RANKER's own number, the one to quote) or {fallback!r} (counts the "
    "never-empty fallback's position as a hit — inflated, and not a ranker number)."
)
_CARD_RETIRED: Mapping[str, str] = {
    k: _READING_HELP.format(k=k, ranker="ranker", fallback="with_fallback")
    for k in ("after_fix", "before_fix")
}
_TASK_RETIRED: Mapping[str, str] = {
    "rank": _READING_HELP.format(
        k="rank", ranker="rank_ranker", fallback="rank_with_fallback"
    ),
    "rank_before_fix": _READING_HELP.format(
        k="rank_before_fix", ranker="rank_ranker", fallback="rank_with_fallback"
    ),
    "hit": _READING_HELP.format(
        k="hit", ranker="hit_ranker", fallback="hit_with_fallback"
    ),
}


class _StrictCard(dict):
    """A result mapping that REFUSES the retired, reading-ambiguous key names.

    Redefining ``after_fix`` in place would have re-pointed every existing report at a
    different number with nothing in the diff to notice; refusing the name makes each call
    site say which reading it wants.
    """

    def __init__(self, data: Mapping[str, Any], retired: Mapping[str, str]) -> None:
        super().__init__(data)
        self._retired = retired

    def _refuse(self, key: Any) -> None:
        if key in self._retired:
            raise AmbiguousMetric(self._retired[key])

    def __getitem__(self, key: Any) -> Any:
        self._refuse(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._refuse(key)
        return super().get(key, default)


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
    """Retrieval scorecard over a golden set — recall@k / MRR under BOTH readings.

    Positive tasks (``expect_ops`` non-empty): a hit is ``min rank`` over ANY expected op
    (the ambiguous-intent adapter). Out-of-scope tasks (``expect_ops == []``): correct iff
    the top-1 is empty or below the confidence floor (a score-0 fallback), never a
    confident false positive.

    TWO READINGS, NEVER ONE. The 0/97 never-empty fallback appends a score-0 candidate when
    nothing genuinely matched, so a gold op can appear in the list without the ranker having
    found it:

    * ``ranker`` — genuine hits only. This is the ranker's own number and the one to quote.
    * ``with_fallback`` — credits the fallback's position as a hit. It measures the fallback,
      not the ranker, and it lies in two directions: it reads FLAT across k (the target is at
      rank 1 or it was manufactured), and it manufactures regressions — a real ranker
      improvement that stops the fallback from firing shows up as recall going DOWN.

    Both are emitted from one scored run; the retired ``before_fix``/``after_fix`` names
    raise :class:`AmbiguousMetric` rather than resolve to either. Score at
    ``limit >= max(RECALL_KS)`` so no true rank is censored below 20.
    """
    per_task: list[dict[str, Any]] = []
    ranks_with_fallback: list[int | None] = []
    ranks_ranker: list[int | None] = []
    oos_pass_ranker: list[bool] = []
    oos_pass_with_fallback: list[bool] = []
    n_via_fallback = 0

    for t in tasks:
        hits = client.search_scored(t.goal, limit=limit)
        if not t.expect_ops:  # out-of-scope
            top1 = hits[0] if hits else None
            genuine_top1 = next((h for h in hits if not h.is_fallback), None)
            pass_ranker = genuine_top1 is None  # the ranker itself declined
            pass_floor = top1 is None or top1.is_fallback  # below the confidence floor
            oos_pass_ranker.append(pass_ranker)
            oos_pass_with_fallback.append(pass_floor)
            per_task.append(
                _StrictCard(
                    {
                        "goal": t.goal,
                        "expect_ops": [],
                        "archetype": t.archetype,
                        "rank_ranker": None,
                        "rank_with_fallback": None,
                        "hit_ranker": pass_ranker,
                        "hit_with_fallback": pass_floor,
                        "via_fallback": False,
                        "top1": top1.name if top1 else None,
                        "top1_is_fallback": bool(top1 and top1.is_fallback),
                    },
                    _TASK_RETIRED,
                )
            )
            continue

        expect = set(t.expect_ops)
        matches = [(i + 1, h) for i, h in enumerate(hits) if h.name in expect]
        rank_with_fallback = min((p for p, _ in matches), default=None)
        genuine = [p for p, h in matches if not h.is_fallback]
        rank_ranker = min(genuine) if genuine else None
        via_fallback = rank_with_fallback is not None and rank_ranker is None
        n_via_fallback += int(via_fallback)
        ranks_with_fallback.append(rank_with_fallback)
        ranks_ranker.append(rank_ranker)
        per_task.append(
            _StrictCard(
                {
                    "goal": t.goal,
                    "expect_ops": list(t.expect_ops),
                    "archetype": t.archetype,
                    "rank_ranker": rank_ranker,
                    "rank_with_fallback": rank_with_fallback,
                    "hit_ranker": rank_ranker is not None,
                    "hit_with_fallback": rank_with_fallback is not None,
                    "via_fallback": via_fallback,
                },
                _TASK_RETIRED,
            )
        )

    n_oos = len(oos_pass_with_fallback) or 1
    return _StrictCard(
        {
            "n_positive": len(ranks_ranker),
            "n_oos": len(oos_pass_with_fallback),
            # How much of ``with_fallback`` is not the ranker's — 0 means the two readings
            # coincide on this run, and any gap between them is exactly these tasks.
            "n_via_fallback": n_via_fallback,
            "ranker": _recall_mrr(ranks_ranker),
            "with_fallback": _recall_mrr(ranks_with_fallback),
            "oos_pass_rate": _StrictCard(
                {
                    "ranker": sum(oos_pass_ranker) / n_oos,
                    "with_fallback": sum(oos_pass_with_fallback) / n_oos,
                },
                _CARD_RETIRED,
            ),
            "per_task": per_task,
        },
        _CARD_RETIRED,
    )


def recall_line(block: Mapping[str, Any]) -> str:
    """One reading's ``@k``/MRR cells. Never print this without saying which reading it is."""
    r = block["recall_at"]
    cells = " · ".join(f"@{k} {r[k]:.2f}" for k in RECALL_KS)
    return f"{cells} · MRR {block['mrr']:.3f}"


def recall_summary(card: Mapping[str, Any]) -> str:
    """Both readings, labeled, in one string — so a report cannot quote one alone.

    Reports call THIS rather than formatting a block they picked, which is how the wrong
    number became the headline in the first place.
    """
    n_fb = card["n_via_fallback"]
    return (
        f"- **ranker** (genuine hits only — quote this): {recall_line(card['ranker'])}\n"
        f"- with-fallback (counts the never-empty fallback as a hit; NOT the ranker, "
        f"{n_fb} fallback-only task(s)): {recall_line(card['with_fallback'])}"
    )


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


# --------------------------------------------------------------------------- #
# Tier-classifier gatekeeper (semantic-depth §2.6, §6.1). The FALSIFIER-FIRST artifact:
# it can DISPROVE the tier signal offline before it ships in the live scorer — if the frozen
# golden set cannot clear precision >= 0.95 @ recall >= 0.80, the tier signal does not ship.
# --------------------------------------------------------------------------- #
TIERS: tuple[str, ...] = ("read", "write", "transfer")

# The ship gate itself (semantic-depth §2.6, §6.1) — the tier signal does not ship below it.
TIER_PRECISION_FLOOR = 0.95
TIER_RECALL_FLOOR = 0.80


@dataclass(frozen=True)
class TierEval:
    """The transfer-class precision/recall + the full 3x3 confusion matrix.

    Precision is measured over HIGH-confidence ``transfer`` predictions only — the class that
    can actually BLOCK a paying call (a ``transfer``/low never blocks: 12 + a 35-pt predicate
    = 47 < block_at 60). A low-confidence false positive costs a step_up nudge, not a blocked
    call, so it does not count against the precision floor (semantic-depth §2.6). Recall counts
    any true transfer detected as ``transfer`` (high OR low) — a missed transfer degrades to
    ``write``, the recall miss.

    ``precision``/``recall`` are ``None`` when their denominator is empty — nothing was
    scored, so there is nothing to report. That is NOT 1.0: returning 1.0 for an empty label
    join made a broken join read as a perfect pass against the ship gate. ``gecko.score``
    draws the same line (a measured zero is ``no_difference``, an unmeasurable one is
    ``undetermined``); ``verdict`` applies it here, and an unmeasured gate never ships.
    """

    precision: float | None
    recall: float | None
    confusion: Mapping[tuple[str, str], int]
    transfer_true: int
    transfer_high_pred: int

    @property
    def undetermined_reason(self) -> str:
        """Which half could not be measured, and why. Empty when both were."""
        gaps = []
        if self.precision is None:
            gaps.append(
                "no high-confidence transfer prediction was made, so precision has no "
                "denominator"
            )
        if self.recall is None:
            gaps.append(
                "no labeled transfer op was scored, so recall has no denominator"
            )
        return "; ".join(gaps)

    @property
    def verdict(self) -> str:
        """``ships`` · ``blocked`` · ``undetermined``.

        ``blocked`` outranks ``undetermined``: a half that WAS measured and failed its floor
        settles the question, however little else was scored — the same reason
        ``gecko.score`` reports a determined zero as a result rather than a hedge.
        """
        measured_below = (
            self.precision is not None and self.precision < TIER_PRECISION_FLOOR
        ) or (self.recall is not None and self.recall < TIER_RECALL_FLOOR)
        if measured_below:
            return "blocked"
        if self.precision is None or self.recall is None:
            return "undetermined"
        return "ships"

    def clears_ship_gate(self) -> bool:
        """The one call a gatekeeper should make. False unless BOTH halves were measured
        and both cleared their floor."""
        return self.verdict == "ships"


def load_tier_labels(path: str | Path) -> list[dict[str, str]]:
    """Parse the frozen ``tier_labels.jsonl`` (``{spec, operation_id, tier}`` per line)."""
    rows: list[dict[str, str]] = []
    for lineno, raw in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line:
            continue
        obj = json.loads(line)
        tier = obj.get("tier")
        oid = obj.get("operation_id")
        if tier not in TIERS or not oid:
            raise GoldenError(f"{path}:{lineno}: bad tier label {obj!r}")
        rows.append({"spec": obj.get("spec", ""), "operation_id": oid, "tier": tier})
    return rows


def evaluate_tier(
    operations: Sequence[Operation], labels: Mapping[str, str]
) -> TierEval:
    """Score ``classify_operation`` over the labeled ops. Pure/offline/$0."""
    from .risk import classify_operation

    confusion: dict[tuple[str, str], int] = {}
    transfer_true = transfer_caught = transfer_high_pred = transfer_high_correct = 0
    for op in operations:
        true = labels.get(op.operation_id)
        if true is None:
            continue
        res = classify_operation(op)
        pred = res.tier
        confusion[(true, pred)] = confusion.get((true, pred), 0) + 1
        if true == "transfer":
            transfer_true += 1
            if pred == "transfer":
                transfer_caught += 1
        if pred == "transfer" and res.confidence == "high":
            transfer_high_pred += 1
            if true == "transfer":
                transfer_high_correct += 1
    # None, never 1.0: an empty denominator means the class was never exercised, and a gate
    # cannot be cleared by a measurement that did not happen.
    precision = (
        transfer_high_correct / transfer_high_pred if transfer_high_pred else None
    )
    recall = transfer_caught / transfer_true if transfer_true else None
    return TierEval(precision, recall, confusion, transfer_true, transfer_high_pred)
