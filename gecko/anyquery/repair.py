"""One (subject, field) of a plan: its rank-0 source, then the repair walk.

The same rules as the supplier watch (``supplier/repair.py``), on plan data instead of
a hand-written source map, and through the same pieces: ``Runner`` for every call and
its spend, ``drift.check`` against earlier runs' call rows, and ``Resolution`` for the
one-line story in our words.

1. Judge rank 0: not callable, no reply, drifted, or nothing readable at its path.
   Otherwise it stands (``fresh``).
2. Walk ranks 1.. in order. A reply already in hand is reused; a new call is made only
   if ``Runner.affordable`` says it fits the quote. A drifted alternative is skipped.
3. The FIRST alternative with a value decides. It is published (``repaired``) unless
   it differs from the last good value, which is then kept and marked ``stale``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from gecko.orthogonal.errors import ListJobError
from gecko.orthogonal.spec import Target
from gecko.orthogonal.supplier.calls import CallSpec
from gecko.orthogonal.supplier.drift import Baseline, check
from gecko.orthogonal.supplier.field_readers import Reading
from gecko.orthogonal.supplier.repair import Reply, Resolution
from gecko.orthogonal.supplier.runner import Runner
from gecko.orthogonal.supplier.sources import Source

from .calls import NotWiredError, call_for
from .reader import same_value, value_for
from .types import PlannedField, PlannedSource, PriorValue

__all__ = ["CellState", "Outcome", "Prior", "as_source", "resolve_cell"]


@dataclass(frozen=True)
class Prior:
    """What earlier runs of this plan, in this mode, left for one (subject, field)."""

    last_good: PriorValue | None
    #: The candidate that filled the last good value.
    source: str
    #: Set when the latest stored row is stale: the run it first went stale in.
    stale_since: str | None


@dataclass
class CellState:
    """What every cell of one run shares: the runner, the baseline, the replies."""

    run: Runner[Any]
    baseline: Baseline
    targets: dict[tuple[str, str], Target]
    replies: dict[str, Reply] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    resolution: Resolution
    value: str | None
    source: PlannedSource | None


class _Skip(Exception):
    """A source gave nothing usable; the message says why, in our words."""


def as_source(source: PlannedSource, meaning: str) -> Source:
    """A planned source in the supplier core's shape, so ``Resolution`` can tell it."""
    return Source(
        name=source.candidate_id,
        provider=f"{source.api} {source.path}",
        kind="orthogonal" if source.catalog == "orthogonal" else "public",
        price_cents=source.price_cents,
        field_path=source.field_path,
        reads=meaning,
        evidence=source.evidence,
        call_key=source.candidate_id,
    )


def _reply_key(spec: CallSpec) -> str:
    """Two fields that need the same request share one call and one charge."""
    return json.dumps(
        [spec.target.api, spec.path, spec.query, spec.body], sort_keys=True
    )


def _fetch(
    state: CellState, source: PlannedSource, subject: str, *, failover: bool
) -> Reply:
    """The reply for ``source`` and ``subject``: reused if this run already has it.
    A failover asks ``affordable`` first, so it never stops a run on its own."""
    try:
        spec = call_for(source, subject, state.targets)
    except NotWiredError as exc:
        raise _Skip(str(exc)) from None
    except ListJobError as exc:
        raise _Skip(f"not called: {exc.message}") from None
    key = _reply_key(spec)
    held = state.replies.get(key)
    if held is not None:
        return held
    if failover:
        why = state.run.affordable(spec)
        if why is not None:
            raise _Skip(f"not called: {why}")
    start = len(state.run.ledger_rows)
    reply = Reply(state.run.call(spec), state.run.ledger_rows[start:])
    state.replies[key] = reply
    return reply


def _failure(reply: Reply) -> str:
    row = reply.rows[0] if reply.rows else {}
    status = row.get("http_status")
    if row.get("status") not in (None, "ok"):
        return f"HTTP {status}" if isinstance(status, int) else str(row.get("status"))
    if reply.result is None:
        return "no reply"
    return "nothing readable for this field"


def _read(
    state: CellState, source: PlannedSource, planned: PlannedField, reply: Reply
) -> str:
    """The value at the source's path, or ``_Skip`` with the reason (drift first)."""
    if reply.rows:
        drift = check(reply.rows[0], source.field_path, state.baseline)
        if drift is not None:
            raise _Skip(f"drifted ({drift.short})")
    value = value_for(reply.payloads, source.field_path, planned.compare)
    if value is None:
        raise _Skip(_failure(reply))
    return value


def resolve_cell(
    state: CellState, planned: PlannedField, subject: str, prior: Prior | None
) -> Outcome:
    sources = sorted(planned.sources, key=lambda s: s.rank)
    first = sources[0]
    res = Resolution(planned.name, as_source(first, planned.meaning))
    try:
        reply = _fetch(state, first, subject, failover=False)
        value = _read(state, first, planned, reply)
    except _Skip as skip:
        reason = str(skip)
        drifted = reason.startswith("drifted (")
        res.trouble = "drifted" if drifted else "failed"
        res.short = reason[len("drifted (") : -1] if drifted else reason
        res.reason = f"{first.candidate_id}: {reason}"
    else:
        res.filled_by, res.reading = res.first, Reading(value)
        return Outcome(res, value, first)
    snapshot = prior.last_good if prior else None
    chosen: tuple[PlannedSource, str] | None = None
    for source in sources[1:]:
        res.tried.append(source.candidate_id)
        try:
            reply = _fetch(state, source, subject, failover=True)
            got = _read(state, source, planned, reply)
        except _Skip as skip:
            res.skipped.append(f"{source.api} {source.path}: {skip}")
            continue
        chosen = (source, got)
        break
    if chosen is not None:
        source, got = chosen
        before = snapshot.value if snapshot else None
        if before is not None and not same_value(planned.compare, before, got):
            res.refused = True
            res.check = f"{source.api} {source.path} differs from the last good value"
            res.skipped.append(res.check)
        else:
            res.state, res.reading = "repaired", Reading(got)
            res.filled_by = as_source(source, planned.meaning)
            res.check = (
                "agrees with the last good value"
                if before is not None
                else "no last good value to compare"
            )
            return Outcome(res, got, source)
    if snapshot is not None:
        res.state = "stale"
        res.stale_since = (prior.stale_since if prior else None) or state.run.run_id
        if not res.check:
            res.check = "; ".join(res.skipped) or "no other source gave a value"
        return Outcome(res, snapshot.value, None)
    res.state = "no data"
    return Outcome(res, None, None)
