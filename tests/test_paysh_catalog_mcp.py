"""pay.sh catalog MCP — registry build, two-tier freshness, host-pin routing.

Light fakes only: an injected catalog fetcher (a JSON string) and an injected drift probe
(a status-code function). No test touches the network.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from gecko.caller import CallError, build_request
from gecko.catalog_mcp import CatalogMcpSurface
from gecko.paysh_catalog import (
    VERIFIED,
    CatalogRegistry,
    ProbeFn,
    fetch_catalog,
)

# --- A fake catalog: the 2 verified providers + 2 pending, real key shapes. -----------

_COINGECKO_URL = "https://pro-api.coingecko.com/api/v3/x402/onchain"
_PERPLEXITY_URL = "https://pplx.x402.paysponge.com"


def _provider(
    fqn: str, service_url: str, sha: str, **over: object
) -> dict[str, object]:
    base: dict[str, object] = {
        "fqn": fqn,
        "title": fqn.split("/")[-1].title(),
        "service_url": service_url,
        "description": "desc",
        "use_case": "use it",
        "category": "finance",
        "endpoint_count": 3,
        "has_metering": True,
        "has_free_tier": False,
        "min_price_usd": 0.01,
        "max_price_usd": 0.01,
        "sha": sha,
    }
    base.update(over)
    return base


def _catalog_json(providers: list[dict[str, object]]) -> str:
    return json.dumps(
        {"version": 2, "provider_count": len(providers), "providers": providers}
    )


_PROVIDERS = [
    _provider(
        "paysponge/coingecko",
        _COINGECKO_URL,
        "sha-cg-1",
        description="onchain DEX pools token analytics",
        use_case="search onchain dex pools by token",
    ),
    _provider(
        "paysponge/perplexity",
        _PERPLEXITY_URL,
        "sha-px-1",
        description="grounded web answer with citations",
        use_case="cited web search answer",
    ),
    _provider("birdeye/data", "https://public-api.birdeye.so", "sha-be-1"),
    _provider("clustly/tipping", "https://tip.clustly.ai", "sha-cl-1"),
]


def _fetcher(providers: list[dict[str, object]]) -> object:
    payload = _catalog_json(providers)
    return lambda _url: payload


def _registry(providers: list[dict[str, object]] | None = None) -> CatalogRegistry:
    entries = fetch_catalog(fetcher=_fetcher(providers or _PROVIDERS))
    return CatalogRegistry.build(entries)


# --- Build: N surfaces, verified subset vs pending -----------------------------------


def test_registry_builds_one_surface_per_provider() -> None:
    reg = _registry()
    assert len(reg.providers()) == 4
    counts = reg.counts()
    assert counts["verified"] == 2  # coingecko + perplexity
    assert counts["pending"] == 2  # birdeye + clustly
    assert counts["broken"] == 0


def test_pending_provider_is_flagged_not_claimed_correct() -> None:
    reg = _registry()
    birdeye = reg.get("birdeye/data")
    assert birdeye is not None
    assert birdeye.status == "pending"
    op = birdeye.client.spec["paths"]["/"]["get"]
    assert op["x-gecko-comprehension"] == "pending verification"


def test_fetch_drops_malformed_and_non_http_entries() -> None:
    bad = [
        {"fqn": "", "service_url": "https://x.example"},  # no fqn
        {"fqn": "a/b", "service_url": "file:///etc/passwd"},  # non-http (SSRF shape)
        _provider("ok/one", "https://ok.example", "sha-1"),
    ]
    entries = fetch_catalog(fetcher=_fetcher(bad))
    assert [e.fqn for e in entries] == ["ok/one"]


# --- Tier 1: sha-diff re-comprehends ONLY changed providers --------------------------


def test_refresh_recomprehends_only_changed_provider() -> None:
    reg = _registry()
    before = {ps.entry.fqn: ps.client for ps in reg.providers()}

    bumped = [p.copy() for p in _PROVIDERS]
    bumped[2]["sha"] = "sha-be-2"  # only birdeye's content changed
    diff = reg.refresh(fetch_catalog(fetcher=_fetcher(bumped)))

    assert diff.changed == ["birdeye/data"]
    assert diff.added == [] and diff.removed == []
    assert sorted(diff.unchanged) == [
        "clustly/tipping",
        "paysponge/coingecko",
        "paysponge/perplexity",
    ]
    # Unchanged providers keep their EXACT comprehended client (no needless re-work);
    # the changed one is a fresh object.
    after = {ps.entry.fqn: ps.client for ps in reg.providers()}
    assert after["paysponge/coingecko"] is before["paysponge/coingecko"]
    assert after["clustly/tipping"] is before["clustly/tipping"]
    assert after["birdeye/data"] is not before["birdeye/data"]


def test_refresh_adds_and_removes() -> None:
    reg = _registry()
    trimmed = [p for p in _PROVIDERS if p["fqn"] != "clustly/tipping"]
    trimmed.append(_provider("new/provider", "https://new.example", "sha-new-1"))
    diff = reg.refresh(fetch_catalog(fetcher=_fetcher(trimmed)))
    assert diff.added == ["new/provider"]
    assert diff.removed == ["clustly/tipping"]
    assert reg.by_tool("clustly-tipping") is None
    assert reg.get("new/provider") is not None


# --- Tier 2: drift-watch flips verified -> broken on a non-402 -----------------------


def _probe_returning(mapping: dict[str, int | None]) -> ProbeFn:
    """A fake probe keyed by URL host: return the mapped status for that provider's host."""

    def probe(req: object) -> int | None:
        host = (urlsplit(req.url).hostname or "").lower()  # type: ignore[attr-defined]
        return mapping.get(host)

    return probe


def test_drift_watch_only_probes_resolved_endpoints() -> None:
    reg = _registry()
    # Everything answers 402 -> stays verified; pending providers are NOT probed.
    results = reg.drift_watch(
        _probe_returning({"pro-api.coingecko.com": 402, "pplx.x402.paysponge.com": 402})
    )
    probed = {r.fqn for r in results}
    assert probed == {"paysponge/coingecko", "paysponge/perplexity"}
    assert all(r.status == "verified" and not r.changed for r in results)
    assert all(r.probe_status == 402 for r in results)


def test_drift_watch_flips_verified_to_broken_on_non_402() -> None:
    reg = _registry()
    # CoinGecko's path moved -> 404; Perplexity still 402.
    results = reg.drift_watch(
        _probe_returning({"pro-api.coingecko.com": 404, "pplx.x402.paysponge.com": 402})
    )
    by_fqn = {r.fqn: r for r in results}
    assert by_fqn["paysponge/coingecko"].status == "broken"
    assert by_fqn["paysponge/coingecko"].changed is True
    assert reg.get("paysponge/coingecko").status == "broken"  # persisted
    # A broken endpoint is no longer offered as first-call-correct.
    assert reg.counts()["verified"] == 1
    assert reg.counts()["broken"] == 1


def test_drift_watch_recovers_broken_to_verified() -> None:
    reg = _registry()
    reg.drift_watch(
        _probe_returning({"pro-api.coingecko.com": 404, "pplx.x402.paysponge.com": 402})
    )
    assert reg.get("paysponge/coingecko").status == "broken"
    # It answers 402 again -> recovers.
    results = reg.drift_watch(
        _probe_returning({"pro-api.coingecko.com": 402, "pplx.x402.paysponge.com": 402})
    )
    cg = next(r for r in results if r.fqn == "paysponge/coingecko")
    assert cg.status == "verified" and cg.changed is True
    assert reg.get("paysponge/coingecko").last_verified_ts is not None


# --- Per-tool host routing + the auth/host-pin guard ---------------------------------


def test_per_tool_request_routes_to_the_right_host() -> None:
    reg = _registry()
    cg = reg.by_tool("paysponge-coingecko")
    assert cg is not None
    req = cg.client.prepare(
        "paysponge-coingecko", {"query": "jupiter", "network": "solana"}
    )
    assert urlsplit(req.url).hostname == "pro-api.coingecko.com"
    assert req.url.endswith(
        "/api/v3/x402/onchain/search/pools?query=jupiter&network=solana"
    )
    assert req.method == "GET"

    px = reg.by_tool("paysponge-perplexity")
    body = {"model": "sonar", "messages": [{"role": "user", "content": "hi"}]}
    preq = px.client.prepare("paysponge-perplexity", {"body": body})
    assert urlsplit(preq.url).hostname == "pplx.x402.paysponge.com"
    assert preq.url.endswith("/v1/sonar") and preq.method == "POST"


def test_verified_client_pins_trust_anchor_to_its_own_host() -> None:
    reg = _registry()
    cg = reg.by_tool("paysponge-coingecko")
    assert cg.client.anchor.state == "pinned"
    assert cg.client.anchor.trusted_hosts == frozenset({"pro-api.coingecko.com"})
    # A pending provider pins to its catalog service_url host.
    be = reg.by_tool("birdeye-data")
    assert be.client.anchor.trusted_hosts == frozenset({"public-api.birdeye.so"})


def test_auth_host_pin_guard_refuses_off_anchor_injection() -> None:
    # The exfil guard our aggregation relies on: with a pinned host, auth toward ANY other
    # host is refused loudly (the message names only the host, never the secret).
    tool = {
        "name": "op",
        "_invoke": {"method": "GET", "path": "/x", "param_locations": {}},
        "inputSchema": {"type": "object", "properties": {}},
    }
    with pytest.raises(CallError) as exc:
        build_request(
            tool,
            {},
            "https://pro-api.coingecko.com",
            auth={"X-Api-Token": "SECRET"},
            allowed_auth_hosts={
                "pplx.x402.paysponge.com"
            },  # a different provider's anchor
        )
    assert "pro-api.coingecko.com" in str(exc.value)
    assert "SECRET" not in str(exc.value)


# --- Aggregated MCP surface: list_tools, cross-catalog search, routed call ------------


def test_surface_list_tools_is_search_plus_one_ref_per_provider() -> None:
    reg = _registry()
    surface = CatalogMcpSurface(reg)
    tools = surface.list_tools()
    assert tools[0]["name"] == "search_capabilities"
    assert len(tools) == len(reg.providers()) + 1
    names = {t["name"] for t in tools}
    assert {"paysponge-coingecko", "paysponge-perplexity"} <= names


def test_search_capabilities_ranks_across_the_whole_catalog() -> None:
    reg = _registry()
    surface = CatalogMcpSurface(reg)
    hits = surface.search_capabilities("cited web answer with citations")
    assert hits, "expected at least one provider to match"
    top = hits[0]
    assert top["provider"] == "paysponge/perplexity"
    assert top["comprehension"] == "verified"
    assert top["host"] == "https://pplx.x402.paysponge.com"
    assert "inputSchema" in top  # full callable def, first-call-correct


def test_call_tool_routes_recorded_call_to_owning_provider() -> None:
    reg = _registry()
    surface = CatalogMcpSurface(reg)  # default mode="recorded" -> $0, offline
    result = surface.call_tool(
        "paysponge-coingecko", {"query": "jupiter", "network": "solana"}
    )
    assert result["mode"] == "recorded"
    assert urlsplit(result["request"]).hostname == "pro-api.coingecko.com"


def test_call_tool_unknown_name_raises() -> None:
    surface = CatalogMcpSurface(_registry())
    with pytest.raises(CallError):
        surface.call_tool("nope-not-a-provider", {})


def test_verified_shapes_reference_real_provider_fqns() -> None:
    # Guardrail: the verified table must key on fqns that actually appear in the catalog
    # shape, or the shapes silently never attach.
    entries = {e.fqn for e in fetch_catalog(fetcher=_fetcher(_PROVIDERS))}
    assert set(VERIFIED) <= entries
