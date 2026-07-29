"""The metrics 'at a glance' card in ``gecko report`` — measured numbers, honestly labeled.

The Agent-Readiness Scorecard leads with the three provenance-labeled numbers from
:func:`gecko.metrics.compute_metrics`. These tests pin that the rendered HTML shows the
real compression %, the recorded readiness rate, and the correlation score — each with the
honesty label ``metrics.py`` produces — for the committed Pegana fixture, and that the card
leaks no payload or secret.
"""

from __future__ import annotations

from pathlib import Path

from gecko import report
from gecko.ingest import load_spec
from gecko.metrics import compute_metrics

_PEGANA = Path(__file__).parent / "fixtures" / "pegana_p0_openapi.json"


def _metrics():
    return compute_metrics(
        load_spec(str(_PEGANA)),
        raw_source=_PEGANA.read_text(encoding="utf-8"),
    )


def _html() -> str:
    return report.build_scorecard(str(_PEGANA))


def test_card_leads_with_measured_compression_hero() -> None:
    html = _html()
    m = _metrics()
    # the hero: the measured % reduction (e.g. "78.4%")
    assert f"{m.compression.reduction_pct}%" in html
    # the concrete before/after the founder wants ("108KB → 23KB")
    assert "108KB" in html and "23KB" in html
    # honesty: bytes MEASURED, tokens ESTIMATED (chars/4) — never overclaimed
    assert "measured" in html
    assert "estimate" in html.lower()
    assert f"{m.compression.raw_tokens_est:,}" in html
    assert f"{m.compression.surface_tokens_est:,}" in html


def test_card_shows_recorded_readiness_rate() -> None:
    html = _html()
    m = _metrics()
    r = m.readiness
    # X/Y well-formed tools (43/43 for Pegana)
    assert f"{r.well_formed_tools}/{r.total_ops}" in html
    # labeled recorded / structural — NEVER overclaimed as live-verified
    assert "recorded" in html
    assert "NOT live-verified" in html


def test_card_shows_correlation_score_with_provenance() -> None:
    html = _html()
    m = _metrics()
    x = m.correlation
    # DECLARED entities + confirmed cross-API joins are the headline numbers
    assert str(x.declared_entities) in html
    assert str(x.cross_joins_plan_eligible) in html
    # the join surface area + the corroborator-only self-declared domains
    assert str(x.id_shaped_join_fields) in html
    assert str(x.self_declared_domains) in html
    # provenance label carried verbatim — corroborator, never a standalone join
    assert "corroborator" in html


def test_card_numbers_match_compute_metrics() -> None:
    # the single source of truth: the rendered numbers are exactly compute_metrics'.
    html = _html()
    m = _metrics()
    assert m.compression.reduction_pct == 78.4  # guards the committed fixture
    assert m.readiness.well_formed_tools == 43 and m.readiness.total_ops == 43
    # the card must not recompute — it renders what compute_metrics returned.
    assert f"{m.compression.reduction_pct}%" in html
    assert f"{m.readiness.well_formed_tools}/{m.readiness.total_ops}" in html


def test_card_is_control_plane_clean() -> None:
    html = _html().lower()
    for leak in ("authorization", "bearer ", "x-api-key", "secret", "password"):
        assert leak not in html, f"control-plane leak: {leak!r} in metrics card"


def test_metrics_card_sits_after_header_before_dimensions() -> None:
    html = _html()
    header = html.index("Agent-Readiness Scorecard")
    glance = html.index("At a glance")
    dims = html.index("Dimension breakdown")
    assert header < glance < dims
