"""The planner seam: a query becomes a :class:`PlanDraft`, and code decides what survives.

The model only proposes. ``validate_plan`` (lane 1) checks every source against the
catalog, takes every price from the catalog, and raises :class:`PlanRejectedError` when
no field survives: the planner can fail, and says so. No model call happens during a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import AnyQueryConfig
from .types import CandidateSet, PlanDraft, PlannerKind, PlanRequest, QueryPlan

__all__ = [
    "CandidatesFor",
    "FakePlanner",
    "PlanRejectedError",
    "Planner",
    "ValidatePlan",
]


class PlanRejectedError(Exception):
    """No field of the draft survived validation. The message is our words only."""


class Planner(Protocol):
    kind: PlannerKind
    model: str | None

    def draft(self, request: PlanRequest, candidates: CandidateSet) -> PlanDraft: ...


class CandidatesFor(Protocol):
    """The top-``k`` catalog candidates for a request (BM25 over the catalog)."""

    def __call__(
        self, request: PlanRequest, catalog: CandidateSet, k: int = 40
    ) -> CandidateSet: ...


class ValidatePlan(Protocol):
    """Deterministic. Drops (into ``unplanned``) unknown candidate ids, field paths the
    candidate does not declare, missing required inputs, bad literals and
    unwired sources (``storage`` is metadata and drops nothing); overwrites price, catalog, api, path, storage and price_from
    from the candidate; enforces the config caps."""

    def __call__(
        self,
        draft: PlanDraft,
        request: PlanRequest,
        candidates: CandidateSet,
        config: AnyQueryConfig,
        *,
        planner: Planner,
        now: str,
    ) -> QueryPlan: ...


@dataclass
class FakePlanner:
    """Maps known queries to fixed drafts. Recorded mode and the tests use it.

    An unknown query raises :class:`PlanRejectedError` rather than inventing a plan,
    which is the same answer a real planner gives when nothing in the catalog fits.
    """

    drafts: dict[str, PlanDraft] = field(default_factory=dict)
    kind: PlannerKind = "fake"
    model: str | None = None

    def draft(self, request: PlanRequest, candidates: CandidateSet) -> PlanDraft:
        found = self.drafts.get(request.query.strip())
        if found is None:
            raise PlanRejectedError("no plan for this query in the recorded fixtures")
        return found
