"""Tier 1 health, the record half: what a challenge probe saw, written to the registry.

What :func:`record_probe` writes:

* one :class:`OfferObs` per decodable offer (evidence ``EXTRACTED``: a real reply showed
  it);
* the operation's :class:`Health` (tier 1);
* an :class:`Event` when, against the offers held before, a price, a payTo or the set of
  networks changed.

A payTo that changed puts the operation in QUARANTINE (``status="quarantined"``,
``quarantined_until`` = now + :data:`QUARANTINE_HOLD_S`): a payer refuses it
(:func:`gecko.servicemap.pins.pinned_pay_to`) until a later probe, past the hold, sees
the same address again, or the founder confirms it (:func:`pins.confirm_pay_to`). A
first sighting is not a change: there is nothing to compare it to. No reply value is
kept: a challenge is metadata about the route (price, payee, networks).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .probe import (
    DEFAULT_LIMIT,
    HeaderFetch,
    ProbeOutcome,
    ProbeTarget,
    Throttle,
    probe_all,
    probe_targets,
)
from .registry import Registry, RegistryKilledError
from .types import Event, EventKind, Health, HealthStatus, OfferObs

__all__ = [
    "QUARANTINE_HOLD_S",
    "Recorded",
    "SweepReport",
    "record_probe",
    "sweep",
]

#: How long a changed payTo is held before a matching probe may lift it.
QUARANTINE_HOLD_S = 24 * 3600


@dataclass(frozen=True)
class Recorded:
    health: Health
    events: tuple[Event, ...]
    offers: tuple[OfferObs, ...]


def _latest_by_rail(offers: Iterable[OfferObs]) -> dict[tuple[str, str], OfferObs]:
    """The most recently seen offer per (network, asset): what "before" means."""
    latest: dict[tuple[str, str], OfferObs] = {}
    for offer in offers:
        key = (offer.network, offer.asset)
        held = latest.get(key)
        if held is None or (offer.last_seen, offer.pay_to) > (
            held.last_seen,
            held.pay_to,
        ):
            latest[key] = offer
    return latest


def _plus(at: str, seconds: int) -> str:
    moment = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return (moment + dt.timedelta(seconds=seconds)).astimezone(dt.UTC).isoformat()


def _event(
    target: ProbeTarget,
    at: str,
    kind: EventKind,
    before: str | None,
    after: str | None,
) -> Event:
    return Event(
        at=at,
        kind=kind,
        op_id=target.op_id,
        service_id=target.service_id,
        catalog=None,
        before=before,
        after=after,
        note="seen in the x402 challenge (tier 1 probe)",
    )


def _changes(
    target: ProbeTarget,
    held: dict[tuple[str, str], OfferObs],
    seen: dict[tuple[str, str], OfferObs],
    at: str,
) -> tuple[list[Event], bool]:
    events: list[Event] = []
    pay_to_changed = False
    for rail in sorted(set(held) & set(seen)):
        before, after = held[rail], seen[rail]
        if before.pay_to != after.pay_to:
            pay_to_changed = True
            events.append(_event(target, at, "pay_to", before.pay_to, after.pay_to))
        if before.amount != after.amount:
            events.append(
                _event(target, at, "price", str(before.amount), str(after.amount))
            )
    before_nets = sorted({n for n, _ in held})
    after_nets = sorted({n for n, _ in seen})
    if held and seen and before_nets != after_nets:
        events.append(
            _event(target, at, "networks", ",".join(before_nets), ",".join(after_nets))
        )
    return events, pay_to_changed


def _health(
    outcome: ProbeOutcome, prior: Health | None, at: str, pay_to_changed: bool
) -> Health:
    ok = outcome.error is None
    failures = 0 if ok else (prior.consecutive_failures if prior else 0) + 1
    status: HealthStatus
    until: str | None = None
    if pay_to_changed:
        status, until = "quarantined", _plus(at, QUARANTINE_HOLD_S)
    elif prior is not None and prior.status == "quarantined":
        # Lifted only by a matching challenge seen after the hold; anything else holds.
        held_until = prior.quarantined_until
        if ok and held_until is not None and at >= held_until:
            status = "up"
        else:
            status, until = "quarantined", held_until
    elif ok:
        status = "up"
    elif outcome.http_status is not None and outcome.http_status < 500:
        # The host answered, just not with a challenge (free, or wants inputs first).
        status = "unknown"
    else:
        status = "down"
    return Health(
        op_id=outcome.target.op_id,
        status=status,
        tier=1,
        checked_at=at,
        last_ok=at if ok else (prior.last_ok if prior else None),
        last_http_status=outcome.http_status,
        consecutive_failures=failures,
        quarantined_until=until,
    )


def record_probe(
    outcome: ProbeOutcome, *, read: Registry, write: Registry, at: str
) -> Recorded:
    """Compare ``outcome`` with what ``read`` holds and write the result to ``write``
    (the same registry for ``--write``; a memory one for a dry run)."""
    target = outcome.target
    prior = read.health_for(target.op_id)
    held = _latest_by_rail(read.offers_for(target.op_id))
    rows = tuple(
        OfferObs.from_offer(o, op_id=target.op_id, evidence="EXTRACTED", at=at)
        for o in outcome.offers
    )
    seen = _latest_by_rail(rows)
    events, pay_to_changed = _changes(target, held, seen, at)
    health = _health(outcome, prior, at, pay_to_changed)
    if prior is not None and prior.status != health.status:
        events.append(
            dataclasses.replace(
                _event(target, at, "status", prior.status, health.status),
                note="payTo changed: quarantined" if pay_to_changed else "tier 1 probe",
            )
        )
    for row in rows:
        write.put_offer(row)
    write.put_health(health)
    for event in events:
        write.append_event(event)
    return Recorded(health, tuple(events), rows)


@dataclass(frozen=True)
class SweepReport:
    probed: tuple[tuple[ProbeOutcome, Recorded], ...]
    skipped: tuple[str, ...]
    #: Targets past ``limit``, left for the next sweep.
    deferred: int

    def lines(self) -> list[str]:
        """One line per probe, our words and public metadata only."""
        out: list[str] = []
        for outcome, recorded in self.probed:
            t = outcome.target
            head = (
                f"{t.method} {t.url} -> {outcome.http_status} {recorded.health.status}"
            )
            if outcome.error is not None:
                out.append(f"{head}: {outcome.error}")
                continue
            offers = ", ".join(
                f"{o.network} {o.amount} to {o.pay_to}" for o in outcome.offers
            )
            out.append(f"{head}: {offers}")
            out += [
                f"    EVENT {e.kind}: {e.before} -> {e.after}" for e in recorded.events
            ]
        out += [f"skipped {why}" for why in self.skipped]
        if self.deferred:
            out.append(f"deferred {self.deferred} past the limit")
        return out


def sweep(
    *,
    read: Registry,
    write: Registry,
    hosts: Mapping[str, str],
    at: str,
    only: str | None = None,
    limit: int = DEFAULT_LIMIT,
    fetch: HeaderFetch | None = None,
    throttle: Throttle | None = None,
) -> SweepReport:
    """Probe every listed operation of ``hosts`` (at most ``limit``) and record each.
    Refuses before any request when ``write`` has its kill switch engaged."""
    if write.killed():
        raise RegistryKilledError("the registry kill switch is engaged")
    targets, skipped = probe_targets(read, hosts, only=only)
    outcomes = probe_all(targets, fetch=fetch, throttle=throttle, limit=limit)
    probed = tuple(
        (o, record_probe(o, read=read, write=write, at=at)) for o in outcomes
    )
    return SweepReport(probed, tuple(skipped), max(0, len(targets) - max(limit, 0)))
