"""The dataset endpoint's tools: read one saved dataset, never spend, never call a source.

``schema``, ``rows``, ``changes_since`` and ``sources`` (shapes in
:mod:`~gecko.anyquery.dataset_tools`; who may read what in
:mod:`~gecko.anyquery.dataset_read`). A page holds 10 subjects by default (at most 25)
and never more than :data:`~gecko.anyquery.dataset_tools.ANSWER_CAP` characters of JSON;
past either, it ends early with ``next_cursor``. A reader key that sends ``reveal_pii``
gets masked rows and a notice, not an error.

This module imports nothing that pays: no billing, no runner, no service. A test holds
that line.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .dataset_read import (
    ALL_ROWS,
    GOOD,
    DatasetCaller,
    ReadRefusal,
    Shower,
    api_slug,
    cell_view,
    check_args,
    columns_arg,
    decode_cursor,
    diff_rows,
    encode_cursor,
    int_arg,
    json_size,
    latest_version,
    ordered_cells,
    readable,
    reveal_asked,
    subject_arg,
    subject_tag,
)
from .dataset_tools import (
    ANSWER_CAP,
    CELL_STATES,
    DEFAULT_CHANGES,
    DEFAULT_PAGE,
    MAX_CHANGES,
    MAX_PAGE,
    OWNER_TOOLS_ELSEWHERE,
    UNTRUSTED,
)
from .datasets import DatasetError, adopted_runs_and_facts, version_rows
from .history import is_delivered
from .reader_keys import DatasetAccess
from .store import Store, StoreError
from .types import Dataset

__all__ = ["call_dataset_tool"]

_SCHEMA_VERSIONS = 5


def _schema(store: Store, dataset: Dataset, access: DatasetAccess, args: dict) -> dict:
    versions = dataset.versions[-_SCHEMA_VERSIONS:]
    # schema takes no reveal_pii: a subject that is a contact detail is always masked
    # here, and the owner sees it whole in rows with reveal_pii.
    show = Shower(access, False)
    out: dict[str, Any] = {
        "dataset_id": dataset.dataset_id,
        "query": dataset.query,
        "fields": [
            {"name": f.name, "meaning": f.meaning, "compare": f.compare, "pii": f.pii}
            for f in dataset.schema
        ],
        "subjects": [show.subject(s) for s in dataset.subjects],
        "version": dataset.versions[-1].n if dataset.versions else None,
        "versions": [
            {"n": v.n, "materialized_at": v.materialized_at, "counts": dict(v.counts)}
            for v in versions
        ],
    }
    out["pii_masked"] = show.hid
    if not dataset.versions:
        out["note"] = "No version yet: the owner fills it with refresh_dataset."
    return out


def _rows(store: Store, dataset: Dataset, access: DatasetAccess, args: dict) -> dict:
    version = latest_version(dataset)
    columns = columns_arg(dataset, args)
    subject = subject_arg(dataset, args, access)
    status = args.get("status")
    if status is not None and status not in CELL_STATES:
        raise ReadRefusal(
            "bad_arguments", f"status is one of: {', '.join(CELL_STATES)}."
        )
    limit = int_arg(args, "limit", DEFAULT_PAGE, 1, MAX_PAGE) or DEFAULT_PAGE
    show = Shower(access, reveal_asked(args))
    rows = sorted(
        store.dataset_rows(
            dataset.customer, dataset.dataset_id, subject=subject, limit=ALL_ROWS
        ),
        key=lambda r: r.subject,
    )
    if "cursor" in args:
        at, tag = (decode_cursor(args["cursor"]) + [None, None])[:2]
        if at != version:
            raise ReadRefusal(
                "version_changed",
                f"The dataset is at version {version} now; this cursor was of another. "
                "Start again without a cursor.",
            )
        tags = [subject_tag(r.subject) for r in rows]
        if tag not in tags:
            raise ReadRefusal(
                "version_changed", "That cursor no longer matches; restart."
            )
        rows = rows[tags.index(tag) + 1 :]

    answer: dict[str, Any] = {
        "version": version,
        "rows": [],
        "next_cursor": None,
        "pii_masked": False,
        "note": UNTRUSTED,
    }
    # Room for the closing keys (next_cursor, pii_masked, any notice) once filled in.
    used = json_size(answer) + 300
    page: list[dict[str, Any]] = []
    last: str | None = None
    remaining = False
    for row in rows:
        picked = [
            c
            for c in ordered_cells(row, columns)
            if status is None or c.status == status
        ]
        if status is not None and not picked:
            continue
        if len(page) == limit:
            remaining = True
            break
        hid_before = show.hid
        item = {
            "subject": show.subject(row.subject),
            "cells": {c.field: cell_view(c, show) for c in picked},
        }
        cost = json_size(item) + 2  # the ", " between items
        if used + cost > ANSWER_CAP:
            show.hid = hid_before
            if not page:
                raise ReadRefusal(
                    "answer_too_large",
                    "One row is larger than an answer may be; ask for fewer columns.",
                )
            remaining = True
            break
        page.append(item)
        used += cost
        last = row.subject
    answer["rows"] = page
    if remaining and last is not None:
        answer["next_cursor"] = encode_cursor([version, subject_tag(last)])
    answer["pii_masked"] = show.hid
    answer.update(show.extra())
    return answer


def _changes(store: Store, dataset: Dataset, access: DatasetAccess, args: dict) -> dict:
    latest = latest_version(dataset)
    since = int_arg(args, "version", None, 1, 1_000_000)
    target = int_arg(args, "to_version", None, 1, 1_000_000)
    to = latest if target is None else target
    if since is None or since > latest or to > latest:
        raise ReadRefusal("version_not_found", f"Versions are 1..{latest}.")
    if since > to:
        raise ReadRefusal("bad_arguments", "version must not be after to_version.")
    columns = columns_arg(dataset, args)
    subject = subject_arg(dataset, args, access)
    limit = int_arg(args, "limit", DEFAULT_CHANGES, 1, MAX_CHANGES) or DEFAULT_CHANGES
    show = Shower(access, reveal_asked(args))
    start = 0
    if "cursor" in args:
        at = (decode_cursor(args["cursor"]) + [None, None, None])[:3]
        if at[0] != since or at[1] != to or not isinstance(at[2], int):
            raise ReadRefusal(
                "version_changed",
                "That cursor was of other versions; start again without a cursor.",
            )
        start = at[2]
    try:
        changes = diff_rows(
            version_rows(store, dataset, since),
            version_rows(store, dataset, to),
            columns=columns,
            subject=subject,
        )
    except DatasetError as exc:
        raise ReadRefusal(exc.code, str(exc)) from None

    answer: dict[str, Any] = {
        "from_version": since,
        "to_version": to,
        "changes": [],
        "next_cursor": None,
        "pii_masked": False,
        "note": UNTRUSTED,
    }
    used = json_size(answer) + 300
    page: list[dict[str, Any]] = []
    end = start
    for subject_name, kind, old, new in changes[start:]:
        if len(page) == limit:
            break
        hid_before = show.hid
        item = {
            "subject": show.subject(subject_name),
            "field": new.field,
            "change": kind,
            "value_before_untrusted": show(new.field, old.value),
            "value_after_untrusted": show(new.field, new.value),
            "status_before": old.status,
            "status_after": new.status,
            "source_before": api_slug(old.source) if old.source else None,
            "source_after": api_slug(new.source) if new.source else None,
        }
        item = {k: v for k, v in item.items() if v is not None}
        cost = json_size(item) + 2  # the ", " between items
        if used + cost > ANSWER_CAP and page:
            show.hid = hid_before
            break
        page.append(item)
        used += cost
        end += 1
    answer["changes"] = page
    if end < len(changes):
        answer["next_cursor"] = encode_cursor([since, to, end])
    answer["pii_masked"] = show.hid
    answer.update(show.extra())
    return answer


def _sources(store: Store, dataset: Dataset, access: DatasetAccess, args: dict) -> dict:
    names = [f.name for f in dataset.schema]
    column = args.get("column")
    if column is not None:
        if not isinstance(column, str) or column not in names:
            raise ReadRefusal(
                "unknown_column",
                f"Unknown column. The fields are: {', '.join(names)}.",
            )
        names = [column]
    # Newest plan first: its ranking is the one a refresh runs. A source only an older
    # plan had is listed after it, since it may still have filled cells.
    ranked: dict[str, list[dict[str, Any]]] = {n: [] for n in names}
    seen: dict[str, set[str]] = {n: set() for n in names}
    for plan_id in reversed(dataset.plan_ids):
        plan = store.plan(dataset.customer, plan_id)
        if plan is None:
            continue
        for planned in plan.fields:
            if planned.name not in ranked:
                continue
            for source in sorted(planned.sources, key=lambda s: s.rank):
                if source.candidate_id in seen[planned.name]:
                    continue
                seen[planned.name].add(source.candidate_id)
                ranked[planned.name].append(
                    {
                        "rank": len(ranked[planned.name]),
                        "api": source.api,
                        "path": source.path,
                        "candidate_id": source.candidate_id,
                    }
                )
    runs, facts = adopted_runs_and_facts(store, dataset)
    delivered = {r.run_id for r in runs if is_delivered(r) and r.mode == dataset.mode}
    subjects = set(dataset.subjects)
    asked: dict[str, int] = dict.fromkeys(names, 0)
    filled: dict[tuple[str, str], int] = {}
    last: dict[tuple[str, str], str] = {}
    for fact in facts:
        if fact.run_id not in delivered or fact.subject not in subjects:
            continue
        if fact.field not in asked:
            continue
        asked[fact.field] += 1
        if fact.status in GOOD and fact.source:
            key = (fact.field, fact.source)
            filled[key] = filled.get(key, 0) + 1
            last[key] = max(last.get(key, ""), fact.fetched_at)
    out = []
    for name in names:
        entries = []
        for entry in ranked[name]:
            key = (name, str(entry.pop("candidate_id")))
            entries.append(
                {
                    **entry,
                    "filled": filled.get(key, 0),
                    "last_filled_at": last.get(key),
                }
            )
        out.append({"column": name, "asked": asked[name], "sources": entries})
    return {
        "columns": out,
        "note": (
            "asked counts the cells of this dataset's adopted runs; filled counts the "
            "ones each source filled. Read-only: this endpoint never calls a source and "
            "shows no price."
        ),
    }


_Handler = Callable[[Store, Dataset, DatasetAccess, dict[str, Any]], dict[str, Any]]


_HANDLERS: dict[str, _Handler] = {
    "schema": _schema,
    "rows": _rows,
    "changes_since": _changes,
    "sources": _sources,
}


def call_dataset_tool(
    store: Store, caller: DatasetCaller | None, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """One tool call on the dataset endpoint. Refusals are ``{"error", "code"}``."""
    if name in OWNER_TOOLS_ELSEWHERE:
        return {
            "error": f"{name} is the owner's tool on /anyquery/mcp; this endpoint only reads.",
            "code": "unknown_tool",
        }
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name}", "code": "unknown_tool"}
    try:
        dataset, access = readable(store, caller)
        check_args(name, args)
        return handler(store, dataset, access, args)
    except ReadRefusal as refusal:
        return refusal.to_dict()
    except StoreError:
        return {
            "error": "The dataset store could not answer; try again in a minute.",
            "code": "store_unavailable",
        }
