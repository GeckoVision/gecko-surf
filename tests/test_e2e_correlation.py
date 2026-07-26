"""Phase 4 e2e falsifiable proof — the multi-API correlation system, end to end,
THROUGH the aggregator (``CatalogMcpSurface``), offline and $0 (Pattern B).

The load-bearing chain, on a real 2-API pair sharing a ``solana-token-mint``
value-domain (the Phase-2 producer/consumer fixtures, standing in for the
Birdeye x daily-news testbed):

1. **No DECLARED hint** -> the cross-API token join is a **candidate**, NEVER
   plan-eligible — even when name AND signature both match across the boundary
   (the §13.6 gate), surfaced identically through ``correlate_surfaces`` and
   ``find_correlations``.
2. **One customer-confirmed entity on both sides** -> it jumps to **tier 3,
   plan-eligible**; ``value_domain_index`` groups BOTH providers under the entity;
   and ``cross_plan`` recovers the cross-API chain **first-plan-correct**.
3. **A provider-only declaration** (untrusted x-gecko, no customer confirm) stays a
   **candidate** — the confirmed-gate holds THROUGH the aggregator (an untrusted spec
   can't mint a cross-system plan basis).

Falsified if a bare-name/signature match reaches plan-eligible without a
customer-confirmed DECLARED entity, at any of the aggregator's doors.

The aggregator tools are also asserted ABSENT from ``list_tools`` (the projection
stays byte-identical) yet callable by name.
"""

from __future__ import annotations

from typing import Any

from gecko.caller import CallError
from gecko.catalog_mcp import CatalogMcpSurface
from gecko.client import AgentApiClient
from gecko.compose import Workspace, cross_plan
from gecko.paysh_catalog import CatalogEntry, CatalogRegistry, ProviderSurface
from gecko.surfaces import safe_surface_id

# base58-ish discriminating pattern so BOTH name and signature match across the
# boundary — the hardest case for the §13.6 cross-API gate to hold.
_MINT_PATTERN = "^[1-9A-HJ-NP-Za-km-z]{32,44}$"
_ENTITY = "solanatokenmint"  # normalized form of "solana-token-mint"
_CONFIRMED = {"tokenId": "solana-token-mint"}


def _producer_spec(*, declared: bool) -> dict[str, Any]:
    """Birdeye-like: resolves a token to its mint id (a ``tokenId`` output field)."""
    prop: dict[str, Any] = {"type": "string", "pattern": _MINT_PATTERN}
    if declared:
        prop["x-gecko-entity"] = "solana-token-mint"
    return {
        "openapi": "3.0.0",
        "info": {"title": "Birdeye Token Resolver", "version": "1"},
        "servers": [{"url": "https://public-api.birdeye.so"}],
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


def _consumer_spec(*, declared: bool) -> dict[str, Any]:
    """daily-news-like: prices a token by ``tokenId`` (a required query param)."""
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
        "info": {"title": "daily-news Token Prices", "version": "1"},
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


def _entry(fqn: str, host: str) -> CatalogEntry:
    return CatalogEntry(
        fqn=fqn,
        title=fqn.split("/")[-1].title(),
        service_url=host,
        description="",
        use_case="",
        category="finance",
        endpoint_count=1,
        min_price_usd=0.0,
        max_price_usd=0.0,
        has_free_tier=True,
        has_metering=False,
        sha=fqn,
    )


def _provider(
    spec: dict[str, Any], fqn: str, host: str, *, hints: dict[str, str] | None = None
) -> ProviderSurface:
    slug = safe_surface_id(fqn)
    client = AgentApiClient(
        spec, base_url=host, surface_id=slug, declared_hints=hints or {}
    )
    return ProviderSurface(
        entry=_entry(fqn, host),
        client=client,
        tool_name=slug,
        host=host,
        status="pending",
    )


def _registry(*providers: ProviderSurface) -> CatalogRegistry:
    reg = CatalogRegistry()
    for ps in providers:
        reg._install(ps)
    return reg


def _surface(
    *, producer_declared: bool, consumer_declared: bool, hints: dict[str, str] | None
) -> CatalogMcpSurface:
    """A 2-provider aggregator: a Birdeye-like producer + a daily-news-like consumer."""
    return CatalogMcpSurface(
        _registry(
            _provider(
                _producer_spec(declared=producer_declared),
                "birdeye/data",
                "https://public-api.birdeye.so",
                hints=hints,
            ),
            _provider(
                _consumer_spec(declared=consumer_declared),
                "dailynews/prices",
                "https://prices.example.com",
                hints=hints,
            ),
        )
    )


def _token_link(result: dict[str, Any]) -> dict[str, Any]:
    """The single tokenId->tokenId cross-API link from a correlate_surfaces result."""
    links = [
        lk
        for lk in result["links"]
        if lk["basis"]["src_field"] == "tokenId"
        and lk["basis"]["dst_param"] == "tokenId"
        and lk["basis"]["src_surface"] != lk["basis"]["dst_surface"]
    ]
    assert links, "expected a cross-API tokenId correlation link"
    return links[0]


def _cross_joins(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        j
        for j in payload["cross_joins"]
        if j["producer"]["surface"] != j["consumer"]["surface"]
    ]


# --- PROOF 1: no DECLARED hint -> the cross-API join is a quarantined candidate ----
def test_no_declared_hint_is_candidate_through_aggregator() -> None:
    surface = _surface(producer_declared=False, consumer_declared=False, hints=None)
    # both aggregator doors agree: name+signature match, but NEVER plan-eligible.
    link = _token_link(
        surface.call_tool(
            "correlate_surfaces", {"a": "birdeye/data", "b": "dailynews/prices"}
        )
    )
    assert (
        link["basis"]["tier"] <= 1 and not link["plan_eligible"] and link["candidate"]
    )

    # find_correlations: with NO declared vocab the entity is absent from the index —
    # §13.6 forbids a name-only cross-provider bucket, so there is no join at all.
    fc = surface.call_tool("find_correlations", {"entity": _ENTITY})
    assert fc["group"] is None and _cross_joins(fc) == []


# --- PROOF 2: one confirmed entity -> tier 3, grouped, and cross_plan recovers ------
def test_confirmed_entity_reaches_plan_eligible_and_cross_plan_recovers() -> None:
    surface = _surface(
        producer_declared=False, consumer_declared=False, hints=_CONFIRMED
    )
    link = _token_link(
        surface.call_tool(
            "correlate_surfaces", {"a": "birdeye/data", "b": "dailynews/prices"}
        )
    )
    assert link["basis"]["tier"] == 3 and link["plan_eligible"]
    assert link["basis"]["provenance"] == "DECLARED"

    # value_domain_index groups BOTH providers under the shared entity.
    vdi = surface.call_tool("value_domain_index", {})
    ent = next(e for e in vdi["entities"] if e["entity"] == _ENTITY)
    assert {p["surface"] for p in ent["producers"]} == {"birdeye-data"}
    assert {c["surface"] for c in ent["consumers"]} == {"dailynews-prices"}

    # find_correlations reports the plan-eligible cross-provider join.
    fc = surface.call_tool("find_correlations", {"entity": _ENTITY})
    cross = _cross_joins(fc)
    assert cross and all(j["plan_eligible"] for j in cross)

    # and cross_plan recovers the cross-API chain first-plan-correct.
    graphs = tuple(ps.client.surface_graph for ps in surface.registry.providers())
    ws = Workspace(graphs=graphs)
    plan = cross_plan(ws, "dailynews-prices", "getPrice", set())
    assert plan is not None
    assert [s.surface for s in plan.steps] == ["birdeye-data", "dailynews-prices"]


# --- PROOF 3: a provider-only declaration stays a candidate (confirmed-gate) --------
def test_provider_only_declaration_stays_candidate_through_aggregator() -> None:
    # provider x-gecko on both sides, but NO customer confirmation (hints=None).
    surface = _surface(producer_declared=True, consumer_declared=True, hints=None)

    link = _token_link(
        surface.call_tool(
            "correlate_surfaces", {"a": "birdeye/data", "b": "dailynews/prices"}
        )
    )
    assert link["basis"]["provenance"] == "DECLARED" and link["basis"]["tier"] == 3
    assert not link["plan_eligible"] and link["candidate"]
    assert "provider-declared-unconfirmed" in link["basis"]["signals"]

    # the value-domain index shows the join but marks it a candidate, never confirmed.
    fc = surface.call_tool("find_correlations", {"entity": _ENTITY})
    cross = _cross_joins(fc)
    assert cross, "the provider-declared cross join should be visible as a candidate"
    assert all(not j["plan_eligible"] and j["candidate"] for j in cross)
    assert all(
        not j["producer"]["confirmed"] and not j["consumer"]["confirmed"] for j in cross
    )

    # the confirmed-gate holds all the way to the executable plan: cross_plan refuses it.
    graphs = tuple(ps.client.surface_graph for ps in surface.registry.providers())
    ws = Workspace(graphs=graphs)
    assert cross_plan(ws, "dailynews-prices", "getPrice", set()) is None


# --- the hidden-tool discipline: absent from list_tools, callable by name -----------
def test_aggregator_tools_are_hidden_but_callable() -> None:
    surface = _surface(
        producer_declared=False, consumer_declared=False, hints=_CONFIRMED
    )
    listed = {t["name"] for t in surface.list_tools()}
    assert not (
        {"correlate_surfaces", "find_correlations", "value_domain_index"} & listed
    ), (
        "aggregator correlation tools must NOT be enumerated (projection stays byte-identical)"
    )
    # yet each is callable by name (no raise).
    assert "links" in surface.call_tool(
        "correlate_surfaces", {"a": "birdeye/data", "b": "dailynews/prices"}
    )
    assert "cross_joins" in surface.call_tool("find_correlations", {"entity": _ENTITY})
    assert "entities" in surface.call_tool("value_domain_index", {})


def test_correlate_surfaces_unknown_provider_raises() -> None:
    surface = _surface(producer_declared=False, consumer_declared=False, hints=None)
    try:
        surface.call_tool("correlate_surfaces", {"a": "birdeye/data", "b": "nope"})
    except CallError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover - the raise is the assertion
        raise AssertionError("expected a CallError for an unknown provider")
