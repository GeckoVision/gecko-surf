"""Richer value-domain signature (ingest enrichment, Part A2).

A field whose spec ships NO ``pattern``/``format`` — a bare ``type: string`` mint —
now self-declares its ``base58`` value domain from its name/description/example, so an
intra-API join corroborates where before it had nothing to match on. The gate is NOT
loosened: a cross-API join stays a quarantined CANDIDATE until it is DECLARED **and**
customer-CONFIRMED (§13.6). These tests pin both halves.
"""

from __future__ import annotations

from typing import Any

from gecko.access import public_session
from gecko.client import AgentApiClient
from gecko.graph import _sig_corroborates, _sig_of
from gecko.surface import Surface

_MINT_DESC = "SPL mint address (base58) — the tokens.xyz join key."


# --- the self-declaration: a bare string now carries a value domain ---------------
def test_bare_string_did_not_self_declare_before() -> None:
    # a bare string with no name/description context -> thin signature, no domain.
    assert _sig_of({"type": "string"}) == "string|||"


def test_mint_self_declares_base58_from_description() -> None:
    sig = _sig_of({"type": "string"}, name="mint", description=_MINT_DESC)
    assert sig == "string|base58||"


def test_mint_self_declares_base58_from_example_shape() -> None:
    sig = _sig_of(
        {"type": "string"},
        name="mint",
        example="So11111111111111111111111111111111111111112",
    )
    assert sig == "string|base58||"


def test_explicit_format_wins_over_derived_domain() -> None:
    # an explicit format is never overridden by the derived domain (control preserved).
    sig = _sig_of(
        {"type": "string", "format": "uuid"}, name="mint", description=_MINT_DESC
    )
    assert sig == "string|uuid||"


def test_intra_api_base58_corroborates_where_bare_strings_did_not() -> None:
    bare = _sig_of({"type": "string"})
    assert not _sig_corroborates(bare, bare)  # the before case: nothing to match on
    enriched = _sig_of({"type": "string"}, name="mint", description=_MINT_DESC)
    other = _sig_of({"type": "string"}, name="token_mint", description="base58 mint")
    assert _sig_corroborates(enriched, other)  # now they share a discriminating domain


# --- the gate holds: enrichment never makes an UNCONFIRMED cross-API join plannable -
def _producer_spec(xgecko: dict[str, str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": "Producer", "version": "1.0.0"},
        "servers": [{"url": "https://a.test"}],
        "paths": {
            "/asset": {
                "get": {
                    "operationId": "get_asset",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "mint": {
                                                "type": "string",
                                                "description": _MINT_DESC,
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    if xgecko:
        spec["x-gecko"] = {"entities": xgecko}
    return spec


def _consumer_spec(xgecko: dict[str, str] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": "Consumer", "version": "1.0.0"},
        "servers": [{"url": "https://b.test"}],
        "paths": {
            "/price/{mint}": {
                "get": {
                    "operationId": "get_price",
                    "parameters": [
                        {
                            "name": "mint",
                            "in": "path",
                            "required": True,
                            "description": _MINT_DESC,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    if xgecko:
        spec["x-gecko"] = {"entities": xgecko}
    return spec


def _surface(
    spec: dict[str, Any], sid: str, hints: dict[str, str] | None = None
) -> Surface:
    return Surface.of(
        AgentApiClient(
            spec, session=public_session(), surface_id=sid, declared_hints=hints
        )
    )


def _mint_links(result: Any) -> list[Any]:
    return [lk for lk in result.links if lk.basis.entity]


def test_cross_api_base58_alone_is_not_even_a_link() -> None:
    # NO declaration: base58 corroboration is present but 'mint' has no name-entity, so
    # the cross-API pair produces NO join at all — the enrichment mints nothing.
    a = _surface(_producer_spec(), "prod")
    b = _surface(_consumer_spec(), "cons")
    result = a.correlate(b)
    assert result.summary["plan_eligible"] == 0


def test_cross_api_provider_declared_is_a_candidate_not_plan_eligible() -> None:
    # provider x-gecko declares the entity on BOTH sides: the base58 signature
    # corroborates (a 'format-eq' signal fires) but the join is still a QUARANTINED
    # candidate — an untrusted provider cannot mint a cross-system plan basis.
    ent = {"mint": "solana-token-mint"}
    a = _surface(_producer_spec(ent), "prod")
    b = _surface(_consumer_spec(ent), "cons")
    result = a.correlate(b)
    mint_links = _mint_links(result)
    assert mint_links, "the declared mint pair should surface as a link"
    assert all(not lk.plan_eligible for lk in mint_links)  # the gate holds
    assert result.summary["plan_eligible"] == 0
    # proof the enrichment corroborated the declared join (never a standalone basis).
    assert any("format-eq" in lk.basis.signals for lk in mint_links)


def test_cross_api_customer_confirmed_is_plan_eligible() -> None:
    # ONLY a customer-CONFIRMED declaration (injected hints) opens the plan gate.
    hints = {"mint": "solana-token-mint"}
    a = _surface(_producer_spec(), "prod", hints)
    b = _surface(_consumer_spec(), "cons", hints)
    result = a.correlate(b)
    assert result.summary["plan_eligible"] >= 1
    assert any(lk.plan_eligible for lk in _mint_links(result))
