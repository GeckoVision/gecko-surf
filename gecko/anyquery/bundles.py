"""Bundles: a question saved by name, run again for new subjects without a new plan.

A customer who asks "work email, mobile, title and company for these leads" every week
should not pay for, or wait on, a fresh plan each time the leads change. Saving the plan
as a bundle keeps its fields and ranked sources; :func:`plan_from_bundle` copies them onto
the new subjects. The model is not asked again, so the same bundle prices the same per
subject, and a source that filled a field before is tried first again.

Only a plan whose sources read the subject from the SUBJECT binding can be reused. A plan
whose source carries a subject inside a literal input (a handle typed into a body field)
would ask about the old subject for every new one, so :func:`reuse_problem` refuses it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from .types import Bundle, QueryPlan

__all__ = [
    "BUNDLE_NAME_RE",
    "bundle_view",
    "plan_from_bundle",
    "reuse_problem",
]

BUNDLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def reuse_problem(plan: QueryPlan) -> str | None:
    """Why ``plan`` cannot be run for other subjects, or ``None`` when it can."""
    subjects = [s.lower() for s in plan.subjects if s]
    for planned in plan.fields:
        for source in planned.sources:
            for binding in source.inputs:
                literal = (binding.literal or "").lower()
                if binding.source == "literal" and any(s in literal for s in subjects):
                    return (
                        f"the source for {planned.name} names a subject inside its "
                        f"'{binding.param}' input, so it would keep asking about that "
                        "subject"
                    )
    return None


def plan_from_bundle(
    template: QueryPlan, subjects: Sequence[str], *, name: str, now: str
) -> QueryPlan:
    """``template``'s fields and sources, for ``subjects``: a new plan, the same question."""
    seed = "\n".join(
        [template.customer, "bundle", name, template.plan_id, now, *sorted(subjects)]
    )
    return QueryPlan(
        plan_id="plan-" + hashlib.sha256(seed.encode()).hexdigest()[:20],
        customer=template.customer,
        query=template.query,
        subjects=tuple(subjects),
        fields=template.fields,
        unplanned=template.unplanned,
        planner=template.planner,
        planner_model=template.planner_model,
        catalog_sha=template.catalog_sha,
        created_at=now,
    )


def bundle_view(bundle: Bundle, template: QueryPlan | None) -> dict[str, Any]:
    return {
        "name": bundle.name,
        "query": bundle.query,
        "fields": [] if template is None else [f.name for f in template.fields],
        "saved_from_plan": bundle.plan_id,
        "created_at": bundle.created_at,
    }
