"""``validate_plan``: the model proposed a draft; this decides what survives.

Deterministic, no model, no network. Everything the run will trust is taken from the
candidate, never from the draft: price, catalog, api, path, storage and price_from. A
source the draft names is dropped (and the field lands in ``unplanned`` with our reason
when nothing is left) for any of: an id not among the candidates shown, a pay.sh or other
unwired source, a field path the candidate does not
declare, an input the candidate does not take, a missing required input, a literal that
is long, carries a scheme or trips the sanitizer, or no input taken from the subject.

Path placeholders are inputs too. ``InputBinding.param`` is documented as one of the
candidate's query or body keys; a ``{placeholder}`` in the path is accepted as well, but
only bound to the subject, and only when every subject is a plain domain, because the
Orthogonal core fills a placeholder with a domain and nothing else (``Target.fill``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import get_args

from gecko.orthogonal.spec import DOMAIN_RE
from gecko.orthogonal.supplier.text import untrusted_summary

from ..sanitize import scan_text
from .calls import share_list_inputs
from .config import AnyQueryConfig
from .planner import Planner, PlanRejectedError, ValidatePlan
from .types import (
    CandidateEndpoint,
    CandidateSet,
    Compare,
    InputBinding,
    PlanDraft,
    PlannedField,
    PlannedSource,
    PlanRequest,
    QueryPlan,
    Unplanned,
)

__all__ = ["clean_subjects", "field_name", "validate_plan", "validator_for"]

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")
_COMPARES: tuple[Compare, ...] = get_args(Compare)
_LITERAL_CAP = 200
_SUBJECT_CAP = 200
_MEANING_CAP = 120

WHY_UNKNOWN = "the planner named a source that was not among the catalog candidates"
WHY_PAYSH = "only pay.sh has it, and pay.sh is not wired yet"
WHY_UNWIRED = "the only source is not wired"
WHY_PATH = "no candidate declares that field in its reply"
WHY_INPUTS = "the source needs an input this query does not give"
WHY_DOMAIN = "the source takes a domain in its path, and a subject is not a domain"
WHY_NO_SOURCE = "the planner named no source for it"
WHY_PLANNER = "the planner found no catalog endpoint for it"
WHY_OVER = "over the per-plan field limit"
WHY_NAME = "the field name is not usable"


def field_name(raw: str) -> str | None:
    """``"Day change %"`` -> ``"day_change"``; ``None`` when nothing usable is left."""
    slug = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")[:40].rstrip("_")
    if slug and slug[0].isdigit():
        slug = f"f_{slug}"[:40]
    return slug if _FIELD_RE.match(slug) else None


def clean_subjects(raw: Iterable[str], cap: int) -> tuple[str, ...]:
    """Sanitized, capped, case-insensitively unique. Raises past ``cap`` rather than
    dropping a subject the user named."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = untrusted_summary(item, _SUBJECT_CAP)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    if len(out) > cap:
        raise PlanRejectedError(
            f"at most {cap} subjects per run; this query has {len(out)}"
        )
    return tuple(out)


def _literal_ok(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _LITERAL_CAP
        and "://" not in value
        and value.isprintable()
        and not scan_text(value)
    )


def _inputs(
    bindings: tuple[InputBinding, ...],
    candidate: CandidateEndpoint,
    subjects: tuple[str, ...],
) -> tuple[tuple[InputBinding, ...], str | None]:
    """The cleaned bindings, or ``((), reason)`` when the source cannot be called."""
    placeholders = set(_PLACEHOLDER.findall(candidate.path))
    takes = candidate.query_keys | candidate.body_keys
    kept: dict[str, InputBinding] = {}
    for binding in bindings:
        if binding.param in kept:
            continue
        if binding.param in placeholders:
            if binding.source != "subject":
                return (), WHY_INPUTS
            if not all(DOMAIN_RE.match(s.lower()) for s in subjects):
                return (), WHY_DOMAIN
        elif binding.param not in takes:
            return (), WHY_INPUTS
        if binding.source == "literal":
            if not _literal_ok(binding.literal):
                return (), WHY_INPUTS
            kept[binding.param] = InputBinding(
                binding.param, "literal", binding.literal
            )
        elif binding.source == "subject":
            kept[binding.param] = InputBinding(binding.param, "subject")
        else:
            return (), WHY_INPUTS
    needed = candidate.required_keys | placeholders
    if not needed <= set(kept):
        return (), WHY_INPUTS
    if not any(b.source == "subject" for b in kept.values()):
        # Without the subject every subject gets the same reply, paid once per subject.
        return (), WHY_INPUTS
    return tuple(kept.values()), None


def _why_dropped(candidate: CandidateEndpoint | None, field_path: str) -> str | None:
    if candidate is None:
        return WHY_UNKNOWN
    if not candidate.wired:
        return WHY_PAYSH if candidate.catalog == "paysh" else WHY_UNWIRED
    if field_path not in candidate.response_paths:
        return WHY_PATH
    return None


def _field(
    field: PlannedField,
    name: str,
    by_id: dict[str, CandidateEndpoint],
    subjects: tuple[str, ...],
    config: AnyQueryConfig,
    confirmed: frozenset[tuple[str, str]],
) -> PlannedField | Unplanned:
    kept: list[PlannedSource] = []
    reasons: list[str] = []
    for source in sorted(field.sources, key=lambda s: s.rank):
        if len(kept) >= config.max_sources_per_field:
            break
        candidate = by_id.get(source.candidate_id)
        why = _why_dropped(candidate, source.field_path)
        if why is None and candidate is not None:
            if any(k.candidate_id == candidate.candidate_id for k in kept):
                continue
            inputs, why = _inputs(source.inputs, candidate, subjects)
        if why is not None or candidate is None:
            reasons.append(why or WHY_UNKNOWN)
            continue
        kept.append(
            PlannedSource(
                rank=len(kept),
                candidate_id=candidate.candidate_id,
                catalog=candidate.catalog,
                api=candidate.api,
                path=candidate.path,
                price_cents=candidate.price_cents,
                field_path=source.field_path,
                inputs=inputs,
                storage=candidate.storage,
                evidence=(
                    "confirmed"
                    if (candidate.candidate_id, source.field_path) in confirmed
                    else "declared"
                ),
                price_from=candidate.price_from,
                list_inputs=tuple(
                    sorted(b.param for b in inputs if b.param in candidate.list_inputs)
                ),
            )
        )
    if not kept:
        return Unplanned(name, reasons[0] if reasons else WHY_NO_SOURCE)
    compare: Compare = field.compare if field.compare in _COMPARES else "text"
    meaning = untrusted_summary(field.meaning, _MEANING_CAP) or name.replace("_", " ")
    return PlannedField(name, meaning, compare, tuple(kept))


def _plan_id(request: PlanRequest, now: str, catalog_sha: str, names: list[str]) -> str:
    seed = "\n".join([request.customer, request.query, now, catalog_sha, *names])
    return "plan-" + hashlib.sha256(seed.encode()).hexdigest()[:20]


def validate_plan(
    draft: PlanDraft,
    request: PlanRequest,
    candidates: CandidateSet,
    config: AnyQueryConfig,
    *,
    planner: Planner,
    now: str,
    confirmed: frozenset[tuple[str, str]] = frozenset(),
) -> QueryPlan:
    """The plan that survives, or :class:`PlanRejectedError` when no field does.

    ``confirmed`` holds ``(candidate_id, field_path)`` pairs seen in a live reply; a
    source reading one of them is marked ``evidence="confirmed"``.
    """
    subjects = clean_subjects(request.subjects or draft.subjects, config.max_subjects)
    if not subjects:
        raise PlanRejectedError(
            "name at least one subject (a company domain, a ticker, a URL) to run this on"
        )
    by_id = {c.candidate_id: c for c in candidates.candidates}
    fields: list[PlannedField] = []
    unplanned: list[Unplanned] = []
    names: set[str] = set()
    for field in draft.fields:
        name = field_name(field.name)
        if name is None:
            unplanned.append(Unplanned("unnamed", WHY_NAME))
            continue
        if name in names:
            continue
        names.add(name)
        if len(fields) >= config.max_fields:
            unplanned.append(Unplanned(name, WHY_OVER))
            continue
        result = _field(field, name, by_id, subjects, config, confirmed)
        if isinstance(result, PlannedField):
            fields.append(result)
        else:
            unplanned.append(result)
    for row in draft.unplanned:
        name = field_name(row.field)
        if name is not None and name not in names:
            names.add(name)
            unplanned.append(Unplanned(name, WHY_PLANNER))
    if not fields:
        detail = "; ".join(f"{u.field}: {u.why}" for u in unplanned[:8])
        raise PlanRejectedError(
            "no field of this query can be filled from the catalog"
            + (f" ({detail})" if detail else "")
        )
    return QueryPlan(
        plan_id=_plan_id(
            request, now, candidates.snapshot_sha, [f.name for f in fields]
        ),
        customer=request.customer,
        query=request.query,
        subjects=subjects,
        fields=share_list_inputs(tuple(fields)),
        unplanned=tuple(unplanned),
        planner=planner.kind,
        planner_model=planner.model,
        catalog_sha=candidates.snapshot_sha,
        created_at=now,
    )


def validator_for(confirmed: frozenset[tuple[str, str]]) -> ValidatePlan:
    """:func:`validate_plan` as the service's ``ValidatePlan``, bound to the pairs the
    loaded catalog has seen in a live reply (``LoadedCatalog.confirmed``)."""

    def validate(
        draft: PlanDraft,
        request: PlanRequest,
        candidates: CandidateSet,
        config: AnyQueryConfig,
        *,
        planner: Planner,
        now: str,
    ) -> QueryPlan:
        return validate_plan(
            draft,
            request,
            candidates,
            config,
            planner=planner,
            now=now,
            confirmed=confirmed,
        )

    return validate
