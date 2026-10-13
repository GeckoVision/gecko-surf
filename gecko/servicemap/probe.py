"""Tier 1 health, the request half: read an operation's free x402 challenge.

One probe is one unpaid request to the operation's x402 route. Only the response
HEADERS are read (:func:`gecko.netguard.safe_get_headers`, which validates and pins the
URL and never reads a body); the ``PAYMENT-REQUIRED`` header is decoded with
:func:`gecko.x402_svm.decode_payment_required`. Nothing is ever paid. What a probe saw
is recorded by :mod:`gecko.servicemap.observe`.

Which host serves an operation's x402 route is DATA (``hosts``: catalog -> base URL), so
this module names no provider. Politeness is enforced here: at most one request per
second per host, and at most ``limit`` probes per run.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from gecko.netguard import UnsafeUrlError, safe_get_headers
from gecko.x402_svm import (
    PAYMENT_REQUIRED_HEADER,
    Offer,
    X402Error,
    decode_payment_required,
    header,
)

from .ids import normalize_path
from .registry import Registry

__all__ = [
    "DEFAULT_LIMIT",
    "HeaderFetch",
    "ProbeOutcome",
    "ProbeTarget",
    "Throttle",
    "probe_all",
    "probe_one",
    "probe_targets",
]

#: The design's default ceiling for one founder-run sweep.
DEFAULT_LIMIT = 200
_PLACEHOLDER = "{}"

Method = Literal["GET", "POST"]
#: ``(url, method) -> (status, headers)``. Must never read the body.
HeaderFetch = Callable[[str, Method], tuple[int, Mapping[str, str]]]


def _live_fetch(url: str, method: Method) -> tuple[int, Mapping[str, str]]:
    return safe_get_headers(url, method=method, timeout=15)


@dataclass(frozen=True)
class ProbeTarget:
    op_id: str
    service_id: str
    method: Method
    url: str


@dataclass(frozen=True)
class ProbeOutcome:
    target: ProbeTarget
    http_status: int | None
    offers: tuple[Offer, ...]
    #: Our words: why there is no challenge. ``None`` when one was decoded.
    error: str | None


@dataclass
class Throttle:
    """At most one request per ``interval_s`` per host. Clock and sleep are injected."""

    interval_s: float = 1.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: dict[str, float] = field(default_factory=dict)

    def wait(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        last = self._last.get(host)
        if last is not None:
            gap = self.interval_s - (self.clock() - last)
            if gap > 0:
                self.sleep(gap)
        self._last[host] = self.clock()


def probe_targets(
    registry: Registry,
    hosts: Mapping[str, str],
    *,
    only: str | None = None,
) -> tuple[list[ProbeTarget], list[str]]:
    """Every operation listed by a catalog in ``hosts``, as its x402 route
    ``{host}/{api}{path}``, plus the reasons the others were skipped.

    A path with a placeholder is skipped: filling it needs a subject, and a probe never
    sends one. Only GET and POST are probed (a POST carries ``{}``).
    """
    methods = {o.op_id: o.method for o in registry.operations()}
    targets: dict[str, ProbeTarget] = {}
    skipped: list[str] = []
    for sighting in sorted(
        (s for c in sorted(hosts) for s in registry.sightings(c)),  # type: ignore[arg-type]
        key=lambda s: (s.api, s.path, s.catalog),
    ):
        if only is not None and sighting.api != only:
            continue
        if sighting.op_id in targets:
            continue
        label = f"{sighting.api} {sighting.path}"
        path = normalize_path(sighting.path)
        if _PLACEHOLDER in path:
            skipped.append(f"{label}: path placeholder (a probe sends no subject)")
            continue
        method = methods.get(sighting.op_id, sighting.method).upper()
        if method not in ("GET", "POST"):
            skipped.append(f"{label}: method {method} is not probed")
            continue
        base = hosts[sighting.catalog].rstrip("/")
        targets[sighting.op_id] = ProbeTarget(
            op_id=sighting.op_id,
            service_id=sighting.service_id,
            method="GET" if method == "GET" else "POST",
            url=f"{base}/{sighting.api}{path}",
        )
    return list(targets.values()), skipped


def probe_one(target: ProbeTarget, fetch: HeaderFetch) -> ProbeOutcome:
    """One unpaid request; the challenge decoded, or why there is none."""
    try:
        status, headers = fetch(target.url, target.method)
    except UnsafeUrlError:
        return ProbeOutcome(target, None, (), "the URL failed the network guard")
    except Exception as exc:  # noqa: BLE001 - the type is all that is safe to name
        return ProbeOutcome(target, None, (), f"no answer ({type(exc).__name__})")
    if status != 402:
        return ProbeOutcome(target, status, (), f"HTTP {status}, not a challenge")
    raw = header(headers, PAYMENT_REQUIRED_HEADER)
    try:
        required = decode_payment_required(raw or "")
    except X402Error as exc:
        return ProbeOutcome(target, status, (), str(exc))
    return ProbeOutcome(target, status, required.accepts, None)


def probe_all(
    targets: Sequence[ProbeTarget],
    *,
    fetch: HeaderFetch | None = None,
    throttle: Throttle | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[ProbeOutcome]:
    """At most ``limit`` probes, one per second per host."""
    if limit <= 0:
        return []
    pace = throttle or Throttle()
    send = fetch or _live_fetch
    outcomes: list[ProbeOutcome] = []
    for target in targets[:limit]:
        pace.wait(target.url)
        outcomes.append(probe_one(target, send))
    return outcomes
