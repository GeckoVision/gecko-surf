"""Phase 2 correlation-engine tests (§13, roadmap Phase 2).

The load-bearing pair is the two Pattern-B falsifiable-proof asserts (offline, $0):

1. NO DECLARED hint  -> the cross-API token link is a **candidate** (tier <= 1,
   quarantined), NEVER plan-eligible — even when name AND signature both match, the
   §13.6 gate forbids an auto-join across an API boundary.
2. ONE DECLARED entity confirmed on both sides -> the link jumps to **tier 3,
   plan-eligible**, and ``cross_plan`` recovers the cross-API chain first-plan-correct.

Falsified if a bare-name or signature match reaches tier 3 cross-API without a
DECLARED hint. Both are single asserts.

The remaining tests pin the guardrails defi-security will check: intra tier-2
signature works but cross-API tier-2 does NOT; type is a hard gate; the basis carries
the why; the scorer input is control-plane typed (no traffic field); provider-DECLARED
alone is a candidate; ``.to_dict()`` is JSON-serializable and control-plane clean.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from gecko.client import AgentApiClient
from gecko.compose import Workspace, cross_plan
from gecko.correlate import Correland, CorrelationResult, correlate_surfaces
from gecko.surface import Surface

# base58-ish discriminating pattern so BOTH name and signature match across the
# boundary — the hardest case for the §13.6 gate to hold.
_MINT_PATTERN = "^[1-9A-HJ-NP-Za-km-z]{32,44}$"


def _producer_spec(*, declared: bool) -> dict[str, Any]:
    """Surface A: resolves a token id with no required inputs (stands alone).

    Returns a ``tokenId`` field. ``declared`` adds a provider ``x-gecko-entity``
    marker; a customer confirmation is injected separately via ``declared_hints``.
    """
    prop: dict[str, Any] = {"type": "string", "pattern": _MINT_PATTERN}
    if declared:
        prop["x-gecko-entity"] = "solana-token-mint"
    return {
        "openapi": "3.0.0",
        "info": {"title": "Token Resolver", "version": "1"},
        "servers": [{"url": "https://resolver.example.com"}],
        "paths": {
            "/token/resolve": {
                "get": {
                    "operationId": "resolveToken",
                    "summary": "Resolve a token to its mint id",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "tokenId": prop,
                                            "symbol": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }


def _consumer_spec(*, declared: bool, id_type: str = "string") -> dict[str, Any]:
    """Surface B: prices a token by ``tokenId`` (required query param).

    ``id_type`` lets a test force a type mismatch (integer vs the producer's string).
    """
    schema: dict[str, Any] = {"type": id_type}
    if id_type == "string":
        schema["pattern"] = _MINT_PATTERN
    param: dict[str, Any] = {
        "name": "tokenId",
        "in": "query",
        "required": True,
        "schema": schema,
    }
    if declared:
        param["x-gecko-entity"] = "solana-token-mint"
    return {
        "openapi": "3.0.0",
        "info": {"title": "Token Prices", "version": "1"},
        "servers": [{"url": "https://prices.example.com"}],
        "paths": {
            "/price": {
                "get": {
                    "operationId": "getPrice",
                    "summary": "Get the price of a token",
                    "parameters": [param],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"price": {"type": "number"}},
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
    }


_CONFIRMED = {"tokenId": "solana-token-mint"}


def _surface(
    spec: dict[str, Any], sid: str, *, hints: dict[str, str] | None = None
) -> Surface:
    return Surface.of(AgentApiClient(spec, surface_id=sid, declared_hints=hints or {}))


def _token_link(result: CorrelationResult):
    """The single tokenId->tokenId cross-API link (there is exactly one)."""
    links = [
        link
        for link in result.links
        if link.basis.src_field == "tokenId" and link.basis.dst_param == "tokenId"
    ]
    assert links, "expected a tokenId correlation link"
    return links[0]


# --- the two falsifiable-proof asserts (Pattern B, offline, $0) -----------------
def test_no_declared_hint_stays_candidate() -> None:
    """PROOF 1 — name AND signature match across the boundary, yet the link is a
    quarantined candidate (tier <= 1), NEVER plan-eligible (§13.6)."""
    a = _surface(_producer_spec(declared=False), "resolver")
    b = _surface(_consumer_spec(declared=False), "prices")
    link = _token_link(correlate_surfaces(a, b))
    assert link.basis.tier <= 1 and not link.plan_eligible


def test_one_declared_entity_reaches_tier3_and_cross_plan_recovers() -> None:
    """PROOF 2 — confirm the entity on both sides and the link jumps to tier 3,
    plan-eligible, AND cross_plan recovers the chain first-plan-correct."""
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    link = _token_link(correlate_surfaces(a, b))
    assert link.basis.tier == 3 and link.plan_eligible
    ws = Workspace(graphs=(a.graph, b.graph))
    plan = cross_plan(ws, "prices", "getPrice", set())
    assert plan is not None and [s.surface for s in plan.steps] == [
        "resolver",
        "prices",
    ]


# --- the §13.6 signature gate: intra works, cross does NOT -----------------------
def test_intra_signature_tier2_but_cross_signature_not() -> None:
    """A shared discriminating signature reaches tier 2 INTRA-API, but the SAME
    signature across two surfaces never reaches tier 2 (it falls to a tier-1
    candidate) — the §13.6 cross-API zero-false-high lock."""
    a = _surface(_producer_spec(declared=False), "resolver")
    consumer = _surface(_consumer_spec(declared=False), "prices")
    cross = _token_link(correlate_surfaces(a, consumer))
    assert cross.basis.tier <= 1  # cross-API signature is NOT tier 2

    # Build one surface that both PRODUCES and CONSUMES the signed tokenId so an
    # intra field->param link exists; its signature corroboration reaches tier 2.
    both = _surface(_both_spec(), "one")
    intra = correlate_surfaces(both, both)
    sig_links = [
        link
        for link in intra.links
        if link.basis.tier == 2 and "pattern-eq" in link.basis.signals
    ]
    assert sig_links, "intra signature corroboration should reach tier 2"


def _both_spec() -> dict[str, Any]:
    """One surface: resolveToken produces a signed tokenId; getPrice consumes one."""
    prod = _producer_spec(declared=False)["paths"]["/token/resolve"]
    cons = _consumer_spec(declared=False)["paths"]["/price"]
    return {
        "openapi": "3.0.0",
        "info": {"title": "One", "version": "1"},
        "servers": [{"url": "https://one.example.com"}],
        "paths": {"/token/resolve": prod, "/price": cons},
    }


# --- type is a hard gate --------------------------------------------------------
def test_type_mismatch_drops_to_tier0() -> None:
    """A string tokenId field cannot feed an integer tokenId param — mismatched
    id-type drops to tier 0 (no link), before any name/signature scoring."""
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(
        _consumer_spec(declared=False, id_type="integer"), "prices", hints=_CONFIRMED
    )
    links = [
        link
        for link in correlate_surfaces(a, b).links
        if link.basis.dst_param == "tokenId"
    ]
    assert not links


# --- the basis carries the why --------------------------------------------------
def test_basis_carries_signals_and_provenance() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    basis = _token_link(correlate_surfaces(a, b)).basis
    assert basis.provenance == "DECLARED"
    assert basis.entity == "solanatokenmint"
    assert basis.src_surface == "resolver" and basis.dst_surface == "prices"
    assert any(s.startswith("declared:") for s in basis.signals)
    assert any(s.startswith("declared-by:") for s in basis.signals)
    assert basis.confidence == "high"


# --- provider-DECLARED-only is a candidate, not plan-eligible --------------------
def test_provider_declared_only_is_candidate() -> None:
    """Guardrail 3/4: a provider x-gecko entity comes from an UNTRUSTED spec. It is
    DECLARED but not workspace-confirmed, so the cross-API link is a candidate, not
    plan-eligible — an untrusted provider can't unilaterally mint a cross-system join."""
    a = _surface(_producer_spec(declared=True), "resolver")  # provider x-gecko only
    b = _surface(_consumer_spec(declared=True), "prices")  # provider x-gecko only
    link = _token_link(correlate_surfaces(a, b))
    assert link.basis.provenance == "DECLARED" and link.basis.tier == 3
    assert not link.plan_eligible
    assert "provider-declared-unconfirmed" in link.basis.signals


# --- SECURITY (Graph P2 BLOCKING): plan_eligible is the SOLE plan gate -----------
def test_cross_plan_refuses_provider_only_declared() -> None:
    """The blocking finding: correlate gates a provider-only x-gecko declaration to
    ``plan_eligible=False`` (a candidate), but ``cross_plan`` read the MERGED
    ``graph.declared`` with provenance stripped, so a malicious provider
    self-declaration minted an EXECUTABLE cross-API chain. cross_plan must refuse it
    — an untrusted provider can't unilaterally mint a cross-system plan. Provider-only
    = x-gecko marker present, NO customer confirmation (declared_hints)."""
    a = _surface(_producer_spec(declared=True), "resolver")  # provider x-gecko only
    b = _surface(_consumer_spec(declared=True), "prices")  # provider x-gecko only
    ws = Workspace(graphs=(a.graph, b.graph))
    assert cross_plan(ws, "prices", "getPrice", set()) is None


def test_confirmed_subset_is_customer_only_not_provider() -> None:
    """``graph.confirmed`` is the trusted CUSTOMER-confirmed subset — populated ONLY
    from injected ``declared_hints``, never from provider x-gecko. It is the ONE
    provenance source both correlate and compose read (no per-engine reconstruction)."""
    # provider x-gecko present, but NO customer confirm -> declared has it, confirmed empty
    provider_only = _surface(_producer_spec(declared=True), "resolver").graph
    assert ("tokenid", "solanatokenmint") in provider_only.declared
    assert provider_only.confirmed == ()
    # a customer confirmation -> present in confirmed (the trusted subset)
    confirmed = _surface(
        _producer_spec(declared=False), "resolver", hints=_CONFIRMED
    ).graph
    assert ("tokenid", "solanatokenmint") in confirmed.confirmed


# --- control-plane: the scorer input cannot carry a payload ---------------------
_FORBIDDEN = (
    "value",
    "sample",
    "response",
    "payload",
    "cardinality",
    "overlap",
    "traffic",
    "observed",
    "count",
    "example",
)


def test_correland_is_control_plane_typed() -> None:
    """Guardrail 2: the scorer's ONLY input is a Correland whose typed shape carries
    name/type/value-domain signature/entity/provenance — a response value, sampled
    overlap, or observed cardinality literally cannot be passed in."""
    fields = {f.name for f in dataclasses.fields(Correland)}
    assert fields == {
        "surface_id",
        "op_id",
        "name",
        "id_type",
        "sig",
        "name_entity",
        "declared_entity",
        "declared_source",
        "origin",
    }
    for f in fields:
        for bad in _FORBIDDEN:
            assert bad not in f, f"Correland.{f} looks like a traffic field"


def test_to_dict_is_json_serializable_and_control_plane_clean() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    payload = correlate_surfaces(a, b).to_dict()
    text = json.dumps(payload)  # raises if not serializable
    lowered = text.lower()
    for bad in _FORBIDDEN:
        assert bad not in lowered, f"to_dict leaked a {bad}-shaped key/value"
    assert payload["summary"]["plan_eligible"] >= 1


def test_surface_correlate_facade_matches_free_function() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    assert a.correlate(b).to_dict() == correlate_surfaces(a, b).to_dict()
