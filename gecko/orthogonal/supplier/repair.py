"""Repair: when a field's first source drifts or fails, walk its ranked alternatives.

Per (supplier, mapped field), after the planned calls:

1. Judge the first source: forced off, drifted (``drift.check`` against earlier runs),
   or failed (no reply, or nothing the reader could use). Otherwise it stands.
2. Walk the rest of ``sources.SOURCE_MAP[field]`` in order. A source reads replies
   already in hand when it can (``harvest``); otherwise it costs one call, made only
   if ``Runner.affordable`` says it fits the cap. A drifted alternative is skipped.
3. The FIRST alternative that yields a value decides — no shopping for agreement. It
   is published only if it passes: non-empty, the same entity (its domain, when it
   gives one, is the supplier's), and equal to the last good snapshot when there is
   one. Otherwise the last good value is kept and marked stale since this run.

How a resolution becomes a table row and a ``kind="field"`` ledger row is in
``repair_rows.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..paginate import FetchResult
from .calls import FAILOVER_CALLS, subject_of
from .drift import Baseline, Drift, check
from .field_readers import Reading
from .fields import FIELD_PARENT, NO_PARENT, FieldState
from .gleif import PublicFetch, lookup_parent
from .history import Prior, Snapshot
from .runner import Runner
from .sheet import Supplier
from .sources import FieldSources, Source
from .text import domain_root, normalize_company

Trouble = Literal["drifted", "failed", "forced"]


@dataclass
class Reply:
    """One logical call this run made for one supplier: its result and ledger rows."""

    result: FetchResult | None
    rows: list[dict[str, Any]]

    @property
    def payloads(self) -> list[Any]:
        return list(self.result.payloads) if self.result else []


@dataclass
class RepairContext:
    run: Runner
    baseline: Baseline
    priors: dict[tuple[str, str], Prior]
    forced: frozenset[str]
    gleif_base_url: str
    gleif_fetch: PublicFetch


@dataclass
class Resolution:
    field_name: str
    first: Source
    trouble: Trouble | None = None
    short: str = ""
    reason: str = ""
    tried: list[str] = field(default_factory=list)
    filled_by: Source | None = None
    reading: Reading | None = None
    check: str = ""
    state: FieldState = "fresh"
    snapshot: Snapshot | None = None
    stale_since: str | None = None
    #: A candidate was found and refused: the default row must not show it.
    refused: bool = False
    skipped: list[str] = field(default_factory=list)

    def line(self) -> str:
        """The one-line story, in our words (safe for a tool result)."""
        head = f"{self.field_name}: {self.first.provider} {self.trouble} ({self.short})"
        if self.state == "repaired" and self.filled_by is not None:
            return f"{head} → filled from {self.filled_by.provider}, {self.check}"
        if self.state == "stale":
            return (
                f"{head} → {self.check}; kept the last good value, "
                f"stale since {self.stale_since}"
            )
        tail = "; ".join(self.skipped) or "no other source"
        return f"{head} → {tail}; no data"


def _compact(value: str) -> str:
    text = domain_root(value) if ("." in value and " " not in value) else value
    return re.sub(r"[^a-z0-9]", "", normalize_company(text))


def same_value(field_name: str, before: str, after: str) -> bool:
    """Same entity across runs: company names normalized (suffixes off); a parent
    named by domain matches one named in words by its root (``flex.com`` = Flex)."""
    if field_name == FIELD_PARENT and NO_PARENT in (before, after):
        return before == after
    if field_name == FIELD_PARENT:
        return bool(_compact(before)) and _compact(before) == _compact(after)
    return bool(normalize_company(before)) and (
        normalize_company(before) == normalize_company(after)
    )


def _failure_reason(reply: Reply | None) -> str:
    if reply is None:
        return "not called"
    row = reply.rows[0] if reply.rows else {}
    status = row.get("http_status")
    if row.get("status") not in (None, "ok"):
        return f"HTTP {status}" if isinstance(status, int) else str(row.get("status"))
    return "nothing readable for this field"


def _judge_first(
    ctx: RepairContext,
    fs: FieldSources,
    supplier: Supplier,
    replies: dict[str, Reply],
) -> Resolution:
    first = fs.first
    res = Resolution(fs.field, first)
    if fs.field in ctx.forced:
        res.trouble, res.short = "forced", "forced off for this run"
        res.reason = f"{first.name} forced off for this run (--force-failover)"
        return res
    reply = replies.get(first.call_key)
    drift = _drift_of(ctx, first, reply)
    if drift is not None:
        res.trouble, res.short, res.reason = "drifted", drift.short, drift.reason
        return res
    reading = first.reader(reply.payloads, supplier) if reply and first.reader else None
    if reading is None or (reading.value is None and not fs.empty_is_answer):
        res.trouble, res.short = "failed", _failure_reason(reply)
        res.reason = f"{first.name} failed: {res.short}"
        return res
    res.reading, res.filled_by = reading, first
    return res


def _drift_of(ctx: RepairContext, source: Source, reply: Reply | None) -> Drift | None:
    if reply is None or not reply.rows:
        return None
    return check(reply.rows[0], source.field_path, ctx.baseline)


def _alternative(
    ctx: RepairContext,
    fs: FieldSources,
    source: Source,
    supplier: Supplier,
    replies: dict[str, Reply],
    harvest: list[Any],
    entity_name: str,
) -> Reading:
    """One ranked alternative's reading; ``_Skip`` (noted in ``skipped``) if none."""
    if source.kind == "public":
        result = lookup_parent(
            entity_name, base_url=ctx.gleif_base_url, fetch=ctx.gleif_fetch
        )
        rows = [
            ctx.run.public_ledger_row(
                source.provider, step, subject_of(supplier.domain)
            )
            for step in result.steps
        ]
        drift = check(rows[0], source.field_path, ctx.baseline) if rows else None
        if drift is not None:
            raise _Skip(f"{source.provider} drifted ({drift.short})")
        if result.reading is None:
            raise _Skip(f"{source.provider}: {result.reason}")
        return result.reading
    reply = replies.get(source.call_key)
    drift = _drift_of(ctx, source, reply)
    if drift is not None:
        raise _Skip(f"{source.provider} drifted ({drift.short})")
    reader = source.reader
    if reader is None:
        raise _Skip(f"{source.provider}: no reader")
    pool = harvest if source.harvest else (reply.payloads if reply else [])
    reading = reader(pool, supplier) if pool else None
    if reading is None and reply is None:
        spec = FAILOVER_CALLS[source.call_key](supplier)
        why = ctx.run.affordable(spec)
        if why is not None:
            raise _Skip(f"{source.provider} not called: {why}")
        start = len(ctx.run.ledger_rows)
        reply = Reply(ctx.run.call(spec), ctx.run.ledger_rows[start:])
        replies[source.call_key] = reply
        drift = _drift_of(ctx, source, reply)
        if drift is not None:
            raise _Skip(f"{source.provider} drifted ({drift.short})")
        reading = reader(reply.payloads, supplier)
    if reading is None or (reading.value is None and not fs.empty_is_answer):
        raise _Skip(f"{source.provider} had no value")
    return reading


class _Skip(Exception):
    """An alternative gave nothing usable; the message says why, in our words."""


def resolve(
    ctx: RepairContext,
    fs: FieldSources,
    supplier: Supplier,
    replies: dict[str, Reply],
    harvest: list[Any],
    entity_name: str,
) -> Resolution:
    res = _judge_first(ctx, fs, supplier, replies)
    if res.trouble is None:
        return res
    prior = ctx.priors.get((supplier.name, fs.field))
    res.snapshot = prior.last_good if prior else None
    candidate: tuple[Source, Reading] | None = None
    for source in fs.sources[1:]:
        res.tried.append(source.name)
        try:
            reading = _alternative(
                ctx, fs, source, supplier, replies, harvest, entity_name
            )
        except _Skip as skip:
            res.skipped.append(str(skip))
            continue
        candidate = (source, reading)
        break
    if candidate is not None:
        _judge_candidate(res, fs, supplier, *candidate)
    stale_from = prior.stale_since if prior and prior.stale_since else ctx.run.run_id
    if res.state != "repaired" and res.snapshot is not None:
        res.state, res.stale_since = "stale", stale_from
        if not res.check:
            res.check = "; ".join(res.skipped) or "no other source gave a value"
    elif res.state != "repaired":
        res.state = "no data"
    return res


def _judge_candidate(
    res: Resolution, fs: FieldSources, supplier: Supplier, source: Source, got: Reading
) -> None:
    value = got.value or NO_PARENT
    snap = res.snapshot
    if got.domain and got.domain != supplier.domain:
        res.refused, res.check = True, f"{source.provider} names a different domain"
    elif snap is not None and not same_value(fs.field, snap.value, value):
        res.refused = True
        res.check = f"{source.provider} differs from last snapshot"
    else:
        res.state, res.filled_by, res.reading = "repaired", source, Reading(value)
        res.check = (
            "agrees with last snapshot" if snap else "no last snapshot to compare"
        )
    if res.refused:
        res.skipped.append(res.check)
