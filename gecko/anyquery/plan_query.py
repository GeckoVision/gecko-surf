"""One call from a pasted question to a validated plan and its whole-run quote.

``build_request`` -> ``candidates_for`` (BM25 over the catalogs) -> ``planner.draft``
(the model, or a fake) -> ``validate_plan`` -> ``build_quote``. Nothing here spends:
planning reads the catalog and asks the model; the quote is what a run may cost at most.

Every refusal is a :class:`~gecko.anyquery.planner.PlanRejectedError` in our words, or a
:class:`~gecko.anyquery.billing.QuoteOverCapError` when the plan is fine but too big for
the per-run ceiling. The surface (lane 4) shows either as the answer, not as a failure.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from gecko.orthogonal.supplier.text import untrusted_summary

from ..sanitize import scan_text
from .billing import build_quote
from .config import AnyQueryConfig
from .planner import CandidatesFor, Planner, PlanRejectedError
from .retrieve import bm25_candidates
from .types import CandidateSet, PlanRequest, QueryPlan, Quote
from .validate import clean_subjects, validate_plan

__all__ = ["PlannedQuery", "build_request", "plan_query"]


@dataclass(frozen=True)
class PlannedQuery:
    plan: QueryPlan
    quote: Quote
    candidates_shown: int


def build_request(
    customer: str,
    query: str,
    subjects: tuple[str, ...] | list[str],
    config: AnyQueryConfig,
) -> PlanRequest:
    """The sanitized, capped request. A query that carries an instruction aimed at the
    planner is refused, not cleaned and forwarded."""
    if not isinstance(query, str) or not query.strip():
        raise PlanRejectedError("the query is empty")
    raw = query.strip()[: config.max_query_chars]
    if scan_text(raw):
        raise PlanRejectedError(
            "the query carries an instruction we do not forward to the planner; "
            "rephrase it as the data you want"
        )
    text = untrusted_summary(raw, config.max_query_chars)
    if not text:
        raise PlanRejectedError("the query is empty")
    return PlanRequest(
        customer=customer,
        query=text,
        subjects=clean_subjects(subjects, config.max_subjects),
    )


def plan_query(
    customer: str,
    query: str,
    subjects: tuple[str, ...] | list[str] = (),
    *,
    catalog: CandidateSet,
    planner: Planner,
    config: AnyQueryConfig,
    now: str,
    available_discount: int = 0,
    quote_id: str | None = None,
    candidates_for: CandidatesFor = bm25_candidates,
    k: int = 40,
    confirmed: frozenset[tuple[str, str]] = frozenset(),
) -> PlannedQuery:
    """Plan ``query`` for ``customer`` and quote one run of it."""
    request = build_request(customer, query, subjects, config)
    shown = candidates_for(request, catalog, k)
    if not shown.candidates:
        raise PlanRejectedError("nothing in the catalog matches this query")
    draft = planner.draft(request, shown)
    plan = validate_plan(
        draft, request, shown, config, planner=planner, now=now, confirmed=confirmed
    )
    quote = build_quote(
        plan,
        config,
        available=available_discount,
        quote_id=quote_id or secrets.token_hex(8),
        now=now,
    )
    return PlannedQuery(plan=plan, quote=quote, candidates_shown=len(shown.candidates))
