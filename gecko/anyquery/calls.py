"""A validated plan's sources as the Orthogonal core's data: an allowlist and call specs.

The allowlist of an any-query run is EXACTLY the targets of its plan, built here from
``PlannedSource`` rows that ``validate_plan`` already checked against the catalog. The
key's scope for this run is therefore what the user confirmed, and nothing the fixed
jobs may call leaks into it (``OrthogonalApi.targets``).

Where an input goes is read from the candidate's method: GET -> query, POST -> body.
``InputBinding`` does not say it (see the lane-2 notes), and Orthogonal's GET calls
carry their inputs as query strings, its POST calls as a JSON body. A path placeholder
is filled by a binding of the same name, else by the subject, through ``Target.fill``,
which refuses anything that is not a plain domain.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from gecko.orthogonal.errors import NotAllowedError
from gecko.orthogonal.spec import Target
from gecko.orthogonal.supplier.calls import CallSpec, subject_of

from .types import PlannedField, PlannedSource, QueryPlan

__all__ = [
    "NotWiredError",
    "call_for",
    "distinct_calls",
    "method_of",
    "plan_targets",
    "request_key",
    "share_list_inputs",
]

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class NotWiredError(Exception):
    """The source's catalog cannot be called by a run yet. The message is our words."""


def method_of(source: PlannedSource) -> str:
    """``GET`` or ``POST`` from ``candidate_id`` (``catalog:api:method:path``)."""
    parts = source.candidate_id.split(":", 3)
    method = parts[2].upper() if len(parts) == 4 else ""
    if method not in ("GET", "POST"):
        raise NotAllowedError(f"{source.api} {source.path}: unknown method")
    return method


def _input_names(source: PlannedSource) -> frozenset[str]:
    slots = set(_PLACEHOLDER.findall(source.path))
    return frozenset(b.param for b in source.inputs if b.param not in slots)


def plan_targets(plan: QueryPlan) -> tuple[Target, ...]:
    """One ``Target`` per (api, path template) the plan's Orthogonal sources use.

    Two sources on one endpoint merge: the union of their input names, the higher
    price. Sources of other catalogs add nothing: they are never sent through this key.
    """
    merged: dict[tuple[str, str], Target] = {}
    for source in _orthogonal(s for f in plan.fields for s in f.sources):
        names = _input_names(source)
        is_get = method_of(source) == "GET"
        query, body = (names, frozenset()) if is_get else (frozenset(), names)
        key = (source.api, source.path)
        held = merged.get(key)
        if held is not None:
            query, body = query | held.query_keys, body | held.body_keys
        merged[key] = Target(
            source.api,
            source.path,
            max(source.price_cents, held.pinned_cents if held else 0),
            query_keys=query,
            body_keys=body,
        )
    return tuple(merged[k] for k in sorted(merged))


def request_key(source: PlannedSource, subject: str) -> tuple[str, ...]:
    """The request ``source`` makes for ``subject``, as data: endpoint, subject and the
    values its inputs resolve to (``call_for``'s rule: a literal, else the subject).

    Two sources with one key are one call at run time (``repair._reply_key`` shares the
    reply), so a quote counts keys, not fields. Keyed on the subject even when no input
    reads it, so a quote can only overcount the run, never undercount it.
    """
    parts = source.candidate_id.split(":", 3)
    method = parts[2].upper() if len(parts) == 4 else ""
    values = sorted(
        f"{b.param}={(b.literal or '') if b.source == 'literal' else subject}"
        for b in source.inputs
    )
    return (source.catalog, source.api, method, source.path, subject, *values)


def distinct_calls(plan: QueryPlan) -> dict[tuple[str, ...], int]:
    """Every request a run of ``plan`` could make, failover included, deduplicated,
    with its price in cents: the highest any source on its ENDPOINT lists, because that
    is what ``plan_targets`` makes the runner's guard count for each call to it."""
    sources = [s for f in plan.fields for s in f.sources]
    endpoint_cents: dict[tuple[str, str, str], int] = {}
    for source in sources:
        endpoint = (source.catalog, source.api, source.path)
        endpoint_cents[endpoint] = max(
            endpoint_cents.get(endpoint, 0), source.price_cents
        )
    return {
        request_key(source, subject): endpoint_cents[
            (source.catalog, source.api, source.path)
        ]
        for subject in plan.subjects
        for source in sources
    }


def _orthogonal(sources: Iterable[PlannedSource]) -> Iterable[PlannedSource]:
    return (s for s in sources if s.catalog == "orthogonal")


def share_list_inputs(fields: tuple[PlannedField, ...]) -> tuple[PlannedField, ...]:
    """Every source on one endpoint asks for the union of its list-input literals.

    The planner binds ``include=work_email`` for an email field and ``include=phone``
    for a phone field; as two requests they are two charges (``request_key``). With
    the union they are one request, one charge, one shared reply (``repair._reply_key``),
    and each field still reads its own path from that reply.
    """
    wanted: dict[tuple[str, str, str, str], set[str]] = {}
    for planned in fields:
        for source in planned.sources:
            for b in source.inputs:
                if b.source == "literal" and b.param in source.list_inputs:
                    key = (source.catalog, source.api, source.path, b.param)
                    wanted.setdefault(key, set()).update(
                        _literal(b.literal or "", True)
                    )
    if not wanted:
        return fields

    def merged(source: PlannedSource) -> PlannedSource:
        inputs = tuple(
            replace(
                b,
                literal=", ".join(
                    sorted(wanted[(source.catalog, source.api, source.path, b.param)])
                ),
            )
            if b.source == "literal" and b.param in source.list_inputs
            else b
            for b in source.inputs
        )
        return replace(source, inputs=inputs)

    return tuple(
        replace(planned, sources=tuple(merged(s) for s in planned.sources))
        for planned in fields
    )


def _literal(text: str, as_list: bool) -> str | list[str]:
    """A literal as sent: an array-typed input gets the comma-separated items."""
    if not as_list:
        return text
    return [item.strip() for item in text.split(",") if item.strip()]


def call_for(
    source: PlannedSource, subject: str, targets: dict[tuple[str, str], Target]
) -> CallSpec:
    """The one call ``source`` costs for ``subject``. Raises ``NotWiredError`` for a
    catalog runs cannot call, ``NotAllowedError`` for an input the allowlist refuses."""
    if source.catalog != "orthogonal":
        raise NotWiredError(f"{source.catalog} sources are not wired into runs yet")
    target = targets.get((source.api, source.path))
    if target is None:
        raise NotAllowedError(f"{source.api} {source.path} is not in this plan")
    # A query string carries scalars only (check_allowed), so only a body gets a list.
    is_get = method_of(source) == "GET"
    values: dict[str, Any] = {
        b.param: _literal(b.literal or "", b.param in source.list_inputs and not is_get)
        if b.source == "literal"
        else subject
        for b in source.inputs
    }
    slots = _PLACEHOLDER.findall(source.path)
    path = target.fill(**{name: values.get(name, subject) for name in slots})
    inputs = {k: v for k, v in sorted(values.items()) if k not in slots}
    return CallSpec(
        key=source.candidate_id,
        target=target,
        path=path,
        query=inputs if is_get else {},
        body=None if is_get or not inputs else inputs,
        max_pages=1,
        subject=subject_of(subject),
    )
