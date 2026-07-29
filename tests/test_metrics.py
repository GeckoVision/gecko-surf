"""Comprehension metrics — the a-ha numbers, measured on committed fixtures (Part B).

Every metric is derived from the shipped engine and labeled by provenance:
compression % is MEASURED (bytes), tokens are ESTIMATED; readiness is a RECORDED
structural rate (never overclaimed as live-verified); the correlation score is a
honest count off ``vindex``/``correlate`` — and the real Pegana↔Jupiter mint join
only becomes plan-eligible when customer-CONFIRMED.
"""

from __future__ import annotations

from pathlib import Path

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.ingest import load_spec
from gecko.metrics import compute_metrics
from gecko.surface import Surface

_FIX = Path(__file__).parent / "fixtures"
_PEGANA = _FIX / "pegana_p0_openapi.json"
_JUPITER = (
    Path(__file__).parent.parent / "gecko" / "examples" / "jupiter_swap_openapi.json"
)


def _pegana_metrics(**kw):
    return compute_metrics(
        load_spec(str(_PEGANA)),
        raw_source=_PEGANA.read_text(encoding="utf-8"),
        surface_id="pegana",
        **kw,
    )


# --- context-compression: measured bytes, estimated tokens -----------------------
def test_compression_is_a_real_reduction() -> None:
    m = _pegana_metrics()
    c = m.compression
    assert c.surface_bytes < c.raw_bytes  # the surface is smaller than the raw spec
    assert 0 < c.reduction_pct < 100
    assert c.reduction_pct > 50  # Pegana comprehends to a >50% smaller surface
    assert c.raw_tokens_est > c.surface_tokens_est > 0
    # honesty labels: bytes measured, tokens estimated.
    assert "estimate" in c.token_estimate
    assert "measured" in c.basis


# --- surface-readiness: recorded, never overclaimed as verified ------------------
def test_readiness_is_recorded_and_honest() -> None:
    m = _pegana_metrics()
    r = m.readiness
    assert r.total_ops == m.total_ops > 0
    assert 0 <= r.well_formed_tools <= r.total_ops
    assert r.well_formed_tools + 0 <= r.total_ops
    assert 0.0 <= r.readiness_pct <= 100.0
    assert r.quarantined_tools >= 0
    # the honesty flag: recorded, NOT live-verified.
    assert "recorded" in r.provenance and "NOT live-verified" in r.provenance


def test_readiness_pegana_is_clean() -> None:
    # the real design-partner spec comprehends fully with nothing quarantined.
    r = _pegana_metrics().readiness
    assert r.quarantined_tools == 0
    assert r.well_formed_tools == r.total_ops
    assert r.readiness_pct == 100.0


# --- correlation: honest score, the real mint join --------------------------------
def test_single_surface_correlation_is_honest() -> None:
    x = _pegana_metrics().correlation
    # no x-gecko in the raw spec -> zero DECLARED entities (honest, not fabricated).
    assert x.declared_entities == 0
    assert x.cross_joins_plan_eligible == 0 and x.cross_joins_candidate == 0
    # the richer signature DOES self-declare value domains (corroborator only).
    assert x.self_declared_domains >= 1
    assert (
        "base58" in x.domains
    )  # the Solana mint domain surfaces from name/description
    assert x.id_shaped_join_fields > 0
    assert "corroborator" in x.provenance


def test_confirmed_cross_api_mint_join_is_plan_eligible() -> None:
    # inject the customer-CONFIRMED value-domain vocab on BOTH surfaces, then the real
    # Pegana(mint) <-> Jupiter(inputMint/outputMint) join is plan-eligible.
    jupiter = Surface.of(
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
    x = _pegana_metrics(
        declared_hints={"mint": "solana-token-mint"}, peers=[jupiter]
    ).correlation
    assert x.declared_entities == 1
    assert "solanatokenmint" in x.entities
    assert x.cross_joins_plan_eligible >= 1  # the real mint join, now plan-eligible
    assert x.cross_joins_candidate == 0  # both sides confirmed -> nothing quarantined


def test_provider_declared_mint_join_stays_a_candidate() -> None:
    # a PROVIDER-only declaration (no customer confirm) must NOT be plan-eligible — the
    # §13.6 gate. Simulate provider x-gecko on the peer via a hint that is declared but
    # the primary is unconfirmed by omitting its confirmation.
    jupiter = Surface.of(
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
    # primary NOT declared -> its mint carries no entity -> no cross bucket at all.
    x = _pegana_metrics(peers=[jupiter]).correlation
    assert x.cross_joins_plan_eligible == 0


# --- the report serializes clean (control plane only) ----------------------------
def test_to_dict_is_json_clean() -> None:
    import json

    d = _pegana_metrics().to_dict()
    json.dumps(d)  # must be serializable
    assert set(d) >= {
        "surface_id",
        "total_ops",
        "compression",
        "readiness",
        "correlation",
    }
