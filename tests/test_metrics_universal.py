"""Metrics universality — ``compute_metrics`` runs on EVERY committed provider spec.

The durable guarantee behind "works for every new API": the a-ha metrics pipeline is
API-agnostic, so it comprehends the whole committed provider universe without raising and
yields sane numbers for each — whether the surface compresses (verbose spec) or ENRICHES
(too-sparse spec, ``reduction_pct <= 0``). Offline ($0, Pattern B), deterministic,
control-plane only.

Parametrized over :data:`gecko.provider_matrix.PROVIDERS` (the single source of truth for
the committed universe) so adding a new spec there is the ONLY change needed to cover it —
one line keeps this test true.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from gecko.ingest import load_spec
from gecko.metrics import compute_metrics
from gecko.provider_matrix import PROVIDERS

#: The repo root — this test lives in ``tests/``; PROVIDERS paths are repo-relative.
_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name,rel_path", sorted(PROVIDERS.items()))
def test_compute_metrics_runs_and_is_sane_for_every_committed_spec(
    name: str, rel_path: str
) -> None:
    path = _ROOT / rel_path
    raw_source = path.read_text(encoding="utf-8")

    # (1) it must not raise for ANY committed provider spec — the universality claim.
    m = compute_metrics(load_spec(str(path)), raw_source=raw_source, surface_id=name)

    # (2) sane compression: a FINITE float (may be negative — an enriched, too-sparse spec).
    c = m.compression
    assert isinstance(c.reduction_pct, float)
    assert math.isfinite(c.reduction_pct)
    # the interpretation is self-consistent with the signed truth.
    assert c.is_enriched is (c.reduction_pct <= 0)
    assert c.magnitude_pct == round(abs(c.reduction_pct), 1)
    assert c.raw_bytes > 0 and c.surface_bytes > 0

    # (3) sane readiness: the total is the op count; well-formed never exceeds it.
    r = m.readiness
    assert r.total_ops == m.total_ops
    assert 0 <= r.well_formed_tools <= r.total_ops
    assert 0.0 <= r.readiness_pct <= 100.0


def test_metrics_universe_is_the_full_committed_provider_set() -> None:
    # A tripwire: if a committed provider is added/removed, this count must move WITH it —
    # so the parametrized coverage above can never silently shrink.
    assert len(PROVIDERS) >= 13
