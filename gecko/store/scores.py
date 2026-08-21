"""The observed-only reader — the single source of a PUBLISHED per-endpoint score.

Architecture §3: a score is a five-rate VECTOR with an N-floor, never a bare
0-100. A rate below the floor is ``None`` (NOT EVALUATED) — distinct from a
measured ``0.0`` — because a confident zero on an unexercised endpoint is the
zero-value trap this repo keeps re-hitting.

This module reads ``source == "observed"`` ONLY. Playground and synthetic rows
exist for the funnel, never for a provider's published number; keeping that
filter in one place (here, plus the ``eval_observed`` view in prod) is what
stops an ad-hoc query from leaking a synthetic green into a scorecard.

v1 computes the one rate the recorded/observed :class:`~gecko.corpus.CallOutcome`
feed provides — first-call-correct — and structures the rest of the vector as
``None`` (not-yet-evaluated), to be filled by the routability / derive /
simulate / refusal feeds as they land. Each rate carries its own denominator.
"""

from __future__ import annotations

from dataclasses import dataclass

from .collections import Collection

#: Minimum qualifying rows before a rate is reported. Below it, the rate is None
#: (not evaluated), never 0.0.
N_FLOOR = 20

_OBSERVED = "observed"


@dataclass(frozen=True)
class EndpointScore:
    """The score vector for one (surface, operation) at a pinned spec_rev.

    Every rate is ``float | None``: ``None`` means the endpoint has not been
    exercised enough to say (below :data:`N_FLOOR`), which is a different claim
    from a measured rate of 0.0.
    """

    surface_id: str
    operation_id: str
    spec_rev: str
    n: int  # qualifying observed rows seen for FCC
    first_call_correct: float | None
    # The rest of the vector — filled by their own feeds as they land.
    routability: float | None = None
    derive_readiness: float | None = None
    simulate_land: float | None = None
    refusal: float | None = None


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None below the floor — the zero-value guard in one place."""
    if denominator < N_FLOOR:
        return None
    return round(numerator / denominator, 4)


def endpoint_score(
    collection: Collection,
    *,
    surface_id: str,
    operation_id: str,
    spec_rev: str,
) -> EndpointScore:
    """Project observed outcomes into the FCC rate for one endpoint at a spec_rev.

    Reads ``source == "observed"`` only. The result is reproducible: it is pinned
    to ``spec_rev`` (the immutable defs that produced the calls), so a number can
    always be re-derived against the exact surface that generated it.
    """
    query = {
        "surface_id": surface_id,
        "operation_id": operation_id,
        "surface_rev": spec_rev,
        "source": _OBSERVED,
    }
    rows = list(collection.find(query))
    n = len(rows)
    fcc_hits = sum(1 for row in rows if row.get("first_call_correct") is True)
    return EndpointScore(
        surface_id=surface_id,
        operation_id=operation_id,
        spec_rev=spec_rev,
        n=n,
        first_call_correct=_rate(fcc_hits, n),
    )
