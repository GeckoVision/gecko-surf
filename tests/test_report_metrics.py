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
from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.ingest import load_spec
from gecko.metrics import compute_metrics
from gecko.surface import Surface

_PEGANA = Path(__file__).parent / "fixtures" / "pegana_p0_openapi.json"
_EXAMPLES = Path(__file__).parent.parent / "gecko" / "examples"
_JUPITER = _EXAMPLES / "jupiter_swap_openapi.json"
#: A too-sparse spec: its scorecard must render the ENRICHMENT hero, never "-X% smaller".
_COLOSSEUM = _EXAMPLES / "colosseum_copilot_openapi.json"


def _metrics():
    return compute_metrics(
        load_spec(str(_PEGANA)),
        raw_source=_PEGANA.read_text(encoding="utf-8"),
    )


def _html() -> str:
    return report.build_scorecard(str(_PEGANA))


def _jupiter_surface() -> Surface:
    # The peer surface with the customer-CONFIRMED mint vocabulary on both mint params.
    return Surface.of(
        AgentApiClient(
            str(_JUPITER),
            session=public_session(),
            surface_id="jupiter",
            declared_hints={
                "inputMint": "solana-token-mint",
                "outputMint": "solana-token-mint",
            },
        )
    )


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


def test_enriched_spec_renders_enrichment_hero_not_negative_smaller() -> None:
    # A too-sparse spec (Colosseum) must render the ENRICHMENT hero — "+X% richer" — and
    # NEVER a "-X% smaller" negative. The surface is larger because it's better.
    html = report.build_scorecard(str(_COLOSSEUM))
    m = compute_metrics(
        load_spec(str(_COLOSSEUM)),
        raw_source=_COLOSSEUM.read_text(encoding="utf-8"),
    )
    c = m.compression
    assert c.is_enriched  # guards the fixture stays terse/enriched
    # the honest positive framing: the enrichment LABEL + "richer" unit, POSITIVE magnitude
    assert "Comprehension enrichment" in html
    assert f"+{c.magnitude_pct}%" in html
    assert "richer</span>" in html
    # never the dishonest negative framing for an enriched spec
    assert f"{c.reduction_pct}%<span" not in html  # no signed-negative hero value
    assert "smaller</span>" not in html  # no "-X% smaller" unit
    # still the hero (same visual weight), just a different accent
    assert "metric hero enriched" in html
    # on-thesis copy: too sparse to call correctly, Gecko added the comprehension
    assert "too sparse to call correctly" in html


def test_compressed_spec_still_renders_the_compression_hero() -> None:
    # The verbose case (Pegana) is unchanged: the "-…% smaller" compression hero stays.
    html = _html()
    m = _metrics()
    c = m.compression
    assert not c.is_enriched
    assert f"{c.reduction_pct}%<span" in html  # the signed-positive hero value
    assert "smaller</span>" in html
    assert "Context compression" in html
    # not the enrichment framing (the CSS defines the accent, but nothing renders it)
    assert "Comprehension enrichment" not in html
    assert "metric hero enriched" not in html  # the div class combo, never present


def test_card_shows_recorded_readiness_rate() -> None:
    html = _html()
    m = _metrics()
    r = m.readiness
    # X/Y well-formed tools (43/43 for Pegana)
    assert f"{r.well_formed_tools}/{r.total_ops}" in html
    # labeled recorded / structural — NEVER overclaimed as live-verified
    assert "recorded" in html
    assert "NOT live-verified" in html


def test_single_surface_correlation_reframes_join_surface() -> None:
    # Single-surface: no bald "0 · 0" emptiness — reframe toward the join SURFACE (the
    # potential), still truthful (no fabricated joins).
    html = _html()
    m = _metrics()
    x = m.correlation
    # the join surface area + the corroborator-only self-declared domains are the copy
    assert str(x.id_shaped_join_fields) in html  # 250 id-shaped join keys
    assert str(x.self_declared_domains) in html
    # reframed toward potential, not emptiness
    assert "join keys" in html
    assert "ready to correlate" in html.lower()
    # provenance label carried verbatim — corroborator, never a standalone join
    assert "corroborator" in html
    # honesty: no fabricated cross-provider joins with a single surface
    assert "cross-API joins" not in html


def test_correlated_scorecard_renders_confirmed_cross_joins_ahha() -> None:
    # The a-ha: Pegana(mint) ↔ Jupiter(inputMint/outputMint), both customer-CONFIRMED.
    jupiter = _jupiter_surface()
    html = report.build_scorecard(
        str(_PEGANA), peers=[jupiter], confirmed={"mint": "solana-token-mint"}
    )
    m = compute_metrics(
        load_spec(str(_PEGANA)),
        raw_source=_PEGANA.read_text(encoding="utf-8"),
        declared_hints={"mint": "solana-token-mint"},
        peers=[jupiter],
    )
    n = m.correlation.cross_joins_plan_eligible
    assert n == 42  # guards the real Pegana↔Jupiter join count off the shipped engine
    # the real number, rendered prominently as the second hero
    assert str(n) in html
    assert "cross-API joins" in html
    # plan-eligible + CONFIRMED, provenance-labeled — never a candidate shown as eligible
    assert "Plan-eligible" in html
    assert "CONFIRMED" in html
    assert "DECLARED" in html
    # names the peer surface
    assert "jupiter" in html
    # both sides confirmed -> nothing quarantined here
    assert m.correlation.cross_joins_candidate == 0


def test_correlated_scorecard_is_control_plane_clean() -> None:
    jupiter = _jupiter_surface()
    html = report.build_scorecard(
        str(_PEGANA), peers=[jupiter], confirmed={"mint": "solana-token-mint"}
    ).lower()
    for leak in ("authorization", "bearer ", "x-api-key", "secret", "password"):
        assert leak not in html, f"control-plane leak: {leak!r}"


def test_correlated_scorecard_stays_deterministic() -> None:
    jupiter = _jupiter_surface()
    a = report.build_scorecard(
        str(_PEGANA), peers=[jupiter], confirmed={"mint": "solana-token-mint"}
    )
    b = report.build_scorecard(
        str(_PEGANA), peers=[jupiter], confirmed={"mint": "solana-token-mint"}
    )
    assert a == b


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
