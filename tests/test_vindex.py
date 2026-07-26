"""Phase 3 tests — the multi-provider resolve + value-domain index (§13 Phase 3).

Four Pattern-B falsifiable proofs (offline, $0), plus the guardrails defi-security
will check:

1. ``value_domain_index`` groups the producers/consumers of a shared DECLARED entity
   across ALL loaded surfaces (the cross-provider "what correlates with X" lookup).
2. ``resolve_provider("<name>")`` returns the right surface; an unknown name -> None;
   resolve is scoped to the workspace registry (never a hosted global catalog).
3. A provider-only-declared cross-provider join is PRESENT in the index but marked a
   candidate / NOT plan-eligible; a both-sides-customer-confirmed one IS plan-eligible.
   The index reuses the Phase-2 ``graph.confirmed`` gate — the two layers cannot disagree.
4. Bumping a spec (a new ``surface_rev``) invalidates a cached index (content-addressed).

Control-plane guardrail: the index carries names + entity + provenance ONLY — never a
payload, response value, or secret.
"""

from __future__ import annotations

import json
from typing import Any

from gecko.catalog_mcp import CatalogMcpSurface
from gecko.client import AgentApiClient
from gecko.correlate import correlate_surfaces
from gecko.paysh_catalog import CatalogRegistry, fetch_catalog
from gecko.surface import Surface
from gecko.vindex import IndexCache, index_rev, value_domain_index

_MINT_PATTERN = "^[1-9A-HJ-NP-Za-km-z]{32,44}$"
_ENTITY = "solanatokenmint"  # normalized form of "solana-token-mint"


def _producer_spec(*, declared: bool, field: str = "tokenId") -> dict[str, Any]:
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
                                            field: prop,
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


def _consumer_spec(*, declared: bool) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "pattern": _MINT_PATTERN}
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


# --- Proof 1: the value-domain index groups producers/consumers cross-surface ----
def test_index_groups_producers_and_consumers_across_surfaces() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    c = _surface(_producer_spec(declared=False), "mirror", hints=_CONFIRMED)

    idx = value_domain_index([a, b, c])
    assert _ENTITY in idx.entities()
    group = idx.group(_ENTITY)
    assert group is not None
    producer_surfaces = {ref.surface_id for ref in group.producers}
    consumer_surfaces = {ref.surface_id for ref in group.consumers}
    assert producer_surfaces == {"resolver", "mirror"}
    assert consumer_surfaces == {"prices"}
    # every ref names surface/op/field only — control-plane structure
    for ref in group.producers:
        assert ref.op_id == "resolveToken" and ref.field == "tokenId"


# --- Proof 2: resolve within the workspace registry; unknown -> None --------------
def _registry() -> CatalogRegistry:
    payload = json.dumps(
        {
            "providers": [
                {
                    "fqn": "birdeye/data",
                    "title": "Birdeye",
                    "service_url": "https://public-api.birdeye.so",
                    "description": "token analytics",
                    "use_case": "token data",
                    "category": "finance",
                    "sha": "sha-be-1",
                    "min_price_usd": 0.01,
                    "max_price_usd": 0.01,
                },
                {
                    "fqn": "clustly/tipping",
                    "title": "Clustly",
                    "service_url": "https://tip.clustly.ai",
                    "description": "tipping",
                    "use_case": "tip",
                    "category": "social",
                    "sha": "sha-cl-1",
                    "min_price_usd": 0.01,
                    "max_price_usd": 0.01,
                },
            ]
        }
    )
    return CatalogRegistry.build(fetch_catalog(fetcher=lambda _u: payload))


def test_resolve_provider_and_enumerate_scoped_to_registry() -> None:
    surface = CatalogMcpSurface(_registry())
    assert surface.enumerate_providers() == ["birdeye/data", "clustly/tipping"]

    # exact fqn, tool slug, and human title all resolve to the same provider.
    by_fqn = surface.resolve_provider("birdeye/data")
    by_slug = surface.resolve_provider("birdeye-data")
    by_title = surface.resolve_provider("Birdeye")
    assert by_fqn is not None
    assert by_fqn.entry.fqn == "birdeye/data"
    assert by_slug is by_fqn and by_title is by_fqn

    # an unknown name resolves to None (no fuzzy guessing, no marketplace dump).
    assert surface.resolve_provider("stripe") is None
    assert surface.resolve_provider("") is None


# --- Proof 3: the confirmed gate — provider-only cross join is a candidate ---------
def test_confirmed_cross_join_is_plan_eligible() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    joins = value_domain_index([a, b]).find_correlations(_ENTITY)
    cross = [j for j in joins if j.producer.surface_id != j.consumer.surface_id]
    assert cross and all(j.plan_eligible for j in cross)


def test_provider_only_cross_join_is_candidate_not_plan_eligible() -> None:
    # provider x-gecko on both sides, but NO customer confirmation -> declared, not
    # confirmed. The join must be present (surfaced for a human) but NOT plan-eligible.
    a = _surface(_producer_spec(declared=True), "resolver")
    b = _surface(_consumer_spec(declared=True), "prices")
    joins = value_domain_index([a, b]).find_correlations(_ENTITY)
    cross = [j for j in joins if j.producer.surface_id != j.consumer.surface_id]
    assert cross, "the provider-declared cross join should be visible as a candidate"
    assert all(not j.plan_eligible for j in cross)
    assert all(not j.producer.confirmed and not j.consumer.confirmed for j in cross)


def test_index_join_verdict_matches_phase2_correlate() -> None:
    """The two layers cannot disagree: the index's plan-eligibility for a cross join
    is the SAME verdict Phase-2 ``correlate_surfaces`` returns for that link."""
    a = _surface(_producer_spec(declared=True), "resolver")  # provider-only
    b = _surface(_consumer_spec(declared=True), "prices")
    idx_joins = {
        (j.producer.surface_id, j.consumer.surface_id): j.plan_eligible
        for j in value_domain_index([a, b]).find_correlations(_ENTITY)
    }
    corr = correlate_surfaces(a, b)
    for link in corr.links:
        if link.basis.src_surface == link.basis.dst_surface:
            continue
        key = (link.basis.src_surface, link.basis.dst_surface)
        if key in idx_joins:
            assert idx_joins[key] == link.plan_eligible


# --- Proof 4: content-addressed — a spec bump invalidates a cached index ----------
def test_rev_bumps_when_a_spec_changes() -> None:
    a1 = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    rev1 = index_rev([a1, b])

    # re-comprehend surface A with a materially different spec (added field) -> new
    # surface_rev -> a new index rev.
    a2 = _surface(
        _producer_spec(declared=False, field="tokenMintId"),
        "resolver",
        hints=_CONFIRMED,
    )
    rev2 = index_rev([a2, b])
    assert rev1 != rev2


def test_cache_returns_same_object_until_a_spec_bumps() -> None:
    a1 = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    cache = IndexCache()

    first = cache.get([a1, b])
    again = cache.get([a1, b])
    assert again is first  # unchanged surfaces -> cached, no recompute

    a2 = _surface(
        _producer_spec(declared=False, field="tokenMintId"),
        "resolver",
        hints=_CONFIRMED,
    )
    after = cache.get([a2, b])
    assert after is not first  # a spec bump invalidated the cached index
    assert after.rev != first.rev


def test_confirm_bumps_rev_even_when_spec_is_unchanged() -> None:
    """The declared/confirmed vocab is part of the content address: a re-confirm with
    the identical spec still bumps the rev, so the index is never stale on a confirm."""
    spec = _producer_spec(declared=False)
    a_plain = _surface(spec, "resolver")
    a_confirmed = _surface(spec, "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    assert index_rev([a_plain, b]) != index_rev([a_confirmed, b])


# --- Control-plane: the index is names + entity + provenance, never a payload -----
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
    "secret",
    "token=",
)


def test_index_to_dict_is_control_plane_clean() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    payload = value_domain_index([a, b]).to_dict()
    text = json.dumps(payload).lower()  # raises if not serializable
    for bad in _FORBIDDEN:
        assert bad not in text, f"index to_dict leaked a {bad}-shaped key/value"


def test_index_is_order_independent() -> None:
    a = _surface(_producer_spec(declared=False), "resolver", hints=_CONFIRMED)
    b = _surface(_consumer_spec(declared=False), "prices", hints=_CONFIRMED)
    assert index_rev([a, b]) == index_rev([b, a])
    assert value_domain_index([a, b]).to_dict() == value_domain_index([b, a]).to_dict()
