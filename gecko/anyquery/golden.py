"""The planner's golden set and how a plan is scored against it.

Each golden query lists, per field, the catalog sources that fit (``accept``). An empty
``accept`` means the catalog has nothing that declares the field, so the only correct
answer is to leave it unplanned. Two numbers come out:

* **retrieval recall@k**: of the fields that can be planned, how many have an acceptable
  source among the ``k`` candidates the planner is shown. A miss here is a miss the
  model cannot fix.
* **first-source-correct**: of the fields the golden set names, how many the plan gets
  right: rank-0 source in ``accept``, or honestly unplanned when ``accept`` is empty.
  Over-claiming an unplannable field and planning the wrong source both count as wrong.

Field names in a plan rarely match the golden names exactly ("share_price" vs "price"),
so a plan field is matched to a golden field by name first, then by its rank-0 source
being in that field's ``accept`` list.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .charge import BillingError
from .config import AnyQueryConfig
from .plan_query import plan_query
from .planner import Planner, PlanRejectedError
from .types import CandidateSet, PlanRequest, QueryPlan

__all__ = [
    "FieldOutcome",
    "GoldenQuery",
    "GoldenRun",
    "Outcome",
    "load_golden",
    "retrieval_recall",
    "run_golden",
    "score_plan",
]

Outcome = Literal["correct", "honest_unplanned", "wrong_source", "missing", "overclaim"]
CORRECT: frozenset[Outcome] = frozenset({"correct", "honest_unplanned"})


@dataclass(frozen=True)
class GoldenQuery:
    qid: str
    query: str
    subjects: tuple[str, ...]
    fields: dict[str, frozenset[str]]


@dataclass(frozen=True)
class FieldOutcome:
    qid: str
    field: str
    outcome: Outcome
    picked: str | None

    @property
    def ok(self) -> bool:
        return self.outcome in CORRECT


def load_golden(path: Path) -> tuple[list[GoldenQuery], list[GoldenQuery]]:
    """``(queries, refusals)`` from the golden JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))

    def one(raw: dict[str, object]) -> GoldenQuery:
        fields = raw.get("fields") or {}
        assert isinstance(fields, dict)
        subjects = raw.get("subjects") or []
        assert isinstance(subjects, list)
        return GoldenQuery(
            qid=str(raw["id"]),
            query=str(raw["query"]),
            subjects=tuple(str(s) for s in subjects),
            fields={str(k): frozenset(v) for k, v in fields.items()},
        )

    return [one(q) for q in data["queries"]], [one(q) for q in data.get("refusals", [])]


def retrieval_recall(
    golden: list[GoldenQuery],
    catalog: CandidateSet,
    candidates_for: Callable[[PlanRequest, CandidateSet, int], CandidateSet],
    k: int,
) -> tuple[int, int, list[str]]:
    """``(hits, plannable fields, missed "qid.field")`` at ``k``."""
    hits = total = 0
    missed: list[str] = []
    for item in golden:
        shown = candidates_for(
            PlanRequest("eval", item.query, item.subjects), catalog, k
        )
        ids = {c.candidate_id for c in shown.candidates}
        for name, accept in item.fields.items():
            if not accept:
                continue
            total += 1
            if ids & accept:
                hits += 1
            else:
                missed.append(f"{item.qid}.{name}")
    return hits, total, missed


def score_plan(item: GoldenQuery, plan: QueryPlan | None) -> list[FieldOutcome]:
    """One outcome per golden field. ``plan=None`` means the planner refused."""
    picked: dict[str, str] = {}
    if plan is not None:
        for f in plan.fields:
            picked[f.name] = f.sources[0].candidate_id
    outcomes: list[FieldOutcome] = []
    used: set[str] = set()
    for name, accept in item.fields.items():
        match = name if name in picked else None
        if match is None and accept:
            match = next(
                (n for n, cid in picked.items() if n not in used and cid in accept),
                None,
            )
        if match is None and not accept:
            outcomes.append(FieldOutcome(item.qid, name, "honest_unplanned", None))
            continue
        if match is None:
            outcomes.append(FieldOutcome(item.qid, name, "missing", None))
            continue
        used.add(match)
        cid = picked[match]
        if not accept:
            outcomes.append(FieldOutcome(item.qid, name, "overclaim", cid))
        elif cid in accept:
            outcomes.append(FieldOutcome(item.qid, name, "correct", cid))
        else:
            outcomes.append(FieldOutcome(item.qid, name, "wrong_source", cid))
    return outcomes


@dataclass(frozen=True)
class GoldenRun:
    outcomes: tuple[FieldOutcome, ...]
    #: qid -> the refusal message, for golden queries the planner refused.
    refused: dict[str, str]
    #: qid -> the plan's whole-run quote in micro-USDC, for queries that planned.
    quotes: dict[str, int]
    #: refusal qids that were (correctly) refused.
    refusals_held: tuple[str, ...]
    refusals_broken: tuple[str, ...]

    @property
    def correct(self) -> int:
        return sum(o.ok for o in self.outcomes)


def run_golden(
    queries: list[GoldenQuery],
    refusals: list[GoldenQuery],
    *,
    catalog: CandidateSet,
    planner_for: Callable[[GoldenQuery], Planner],
    config: AnyQueryConfig,
    now: str,
    k: int = 40,
    confirmed: frozenset[tuple[str, str]] = frozenset(),
) -> GoldenRun:
    """Plan every golden query end to end and score it. ``planner_for`` returns the
    planner for one query (a replay of a recorded reply, or the live model)."""
    outcomes: list[FieldOutcome] = []
    refused: dict[str, str] = {}
    quotes: dict[str, int] = {}
    for item in queries:
        plan: QueryPlan | None = None
        try:
            planned = plan_query(
                "eval",
                item.query,
                item.subjects,
                catalog=catalog,
                planner=planner_for(item),
                config=config,
                now=now,
                quote_id=f"q-{item.qid}",
                k=k,
                confirmed=confirmed,
            )
            plan = planned.plan
            quotes[item.qid] = planned.quote.total_usdc_micro
        except (PlanRejectedError, BillingError) as exc:
            refused[item.qid] = str(exc)
        outcomes.extend(score_plan(item, plan))
    held: list[str] = []
    broken: list[str] = []
    for item in refusals:
        try:
            plan_query(
                "eval",
                item.query,
                item.subjects,
                catalog=catalog,
                planner=planner_for(item),
                config=config,
                now=now,
                quote_id=f"q-{item.qid}",
                k=k,
                confirmed=confirmed,
            )
            broken.append(item.qid)
        except (PlanRejectedError, BillingError):
            held.append(item.qid)
    return GoldenRun(tuple(outcomes), refused, quotes, tuple(held), tuple(broken))
