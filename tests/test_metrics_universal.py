"""Universality — ``compute_metrics`` + the scorecard still work on ALL committed specs.

The tripwire for ingestion changes: slice 2 adds body/response-field decomposition at
ingest, which every ``Surface`` build runs. This sweeps the whole committed provider
universe (``provider_matrix.PROVIDERS``, 14 specs) and asserts each still comprehends into
a Metrics + an HTML scorecard without raising — deterministic and $0 (Pattern B). A spec
that stops comprehending, or a scorecard that stops rendering, trips here.
"""

from __future__ import annotations

from pathlib import Path

from gecko.ingest import load_spec
from gecko.metrics import compute_metrics
from gecko.provider_matrix import MINT_HINTS, PROVIDERS
from gecko.report import build_scorecard

_ROOT = Path(__file__).resolve().parents[1]


def test_there_are_fourteen_committed_providers() -> None:
    assert len(PROVIDERS) == 14


def test_compute_metrics_works_on_every_committed_spec() -> None:
    for name, rel_path in PROVIDERS.items():
        path = _ROOT / rel_path
        spec = load_spec(str(path))
        m = compute_metrics(
            spec,
            raw_source=path.read_text(encoding="utf-8"),
            surface_id=name,
            declared_hints=MINT_HINTS.get(name),
        )
        assert m.total_ops > 0, f"{name}: comprehended 0 operations"
        # the compression metric computes (byte counts present) — not asserting a positive
        # reduction: a tiny spec can honestly yield a larger tool surface (colosseum).
        assert m.compression.raw_bytes > 0 and m.compression.surface_bytes > 0


def test_scorecard_renders_deterministically_on_every_committed_spec() -> None:
    for name, rel_path in PROVIDERS.items():
        path = _ROOT / rel_path
        first = build_scorecard(str(path), confirmed=MINT_HINTS.get(name))
        second = build_scorecard(str(path), confirmed=MINT_HINTS.get(name))
        assert first == second, f"{name}: scorecard is not byte-stable"
        assert "<html" in first.lower(), f"{name}: scorecard produced no HTML"
