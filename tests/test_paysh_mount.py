"""pay.sh aggregate surface mounts at /paysh/mcp without breaking the other surfaces,
and the single-spec registry-store path TOLERATES the spec-less aggregate.

The catalog surface is an aggregate of many pinned clients with NO single OpenAPI spec, so
this proves the `_surface_spec` / RegistrySurface / discovery path skips it gracefully
instead of crashing, while a normal single-spec surface mounted beside it still initializes.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

import gecko.serve_mcp as serve_mcp  # noqa: E402
from gecko.catalog_mcp import CatalogMcpSurface  # noqa: E402
from gecko.http_server import build_multi_surface_app  # noqa: E402
from gecko.paysh_catalog import CatalogRegistry, fetch_catalog  # noqa: E402

JITO = "examples/jito_demo/spec/jito_openapi.json"

_PROVIDERS = [
    {
        "fqn": "paysponge/coingecko",
        "title": "Coingecko",
        "service_url": "https://pro-api.coingecko.com/api/v3/x402/onchain",
        "description": "onchain DEX pools",
        "use_case": "search onchain dex pools by token",
        "category": "finance",
        "endpoint_count": 3,
        "min_price_usd": 0.01,
        "max_price_usd": 0.01,
        "sha": "sha-cg-1",
    },
    {
        "fqn": "birdeye/data",
        "title": "Birdeye",
        "service_url": "https://public-api.birdeye.so",
        "description": "token market data",
        "use_case": "token prices",
        "category": "finance",
        "endpoint_count": 5,
        "min_price_usd": 0.02,
        "max_price_usd": 0.05,
        "sha": "sha-be-1",
    },
]


def _catalog_surface() -> CatalogMcpSurface:
    entries = fetch_catalog(
        fetcher=lambda _url: json.dumps({"version": 2, "providers": _PROVIDERS})
    )
    return CatalogMcpSurface(CatalogRegistry.build(entries))


def _app():
    return build_multi_surface_app(
        [("paysh", _catalog_surface()), ("jito", JITO)],
        public_url="https://mcp.example.com",
        allowed_hosts=["testserver"],
    )


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def test_paysh_and_jito_both_initialize_under_the_mount() -> None:
    with TestClient(_app()) as c:
        for name in ("paysh", "jito"):
            r = c.post(f"/{name}/mcp", json=_INIT, headers=_HEADERS)
            assert r.status_code == 200, f"{name}/mcp init failed: {r.status_code}"


def test_root_index_lists_paysh_alongside_jito() -> None:
    with TestClient(_app()) as c:
        idx = c.get("/").json()
        assert {s["name"] for s in idx["surfaces"]} == {"paysh", "jito"}


def test_jito_discovery_still_works_next_to_the_spec_less_aggregate() -> None:
    # jito keeps its spec-derived discovery routes; the aggregate simply doesn't emit them.
    with TestClient(_app()) as c:
        assert c.get("/jito/llms.txt").status_code == 200
        assert "sendBundle" in c.get("/jito/tools.md").text
        # the aggregate has no single spec, so its llms.txt is absent (graceful skip),
        # never a crash.
        assert c.get("/paysh/llms.txt").status_code == 404


# --- The registry-store single-spec path tolerates the aggregate ----------------------


def test_surface_spec_returns_none_for_the_aggregate() -> None:
    assert serve_mcp._surface_spec(_catalog_surface()) is None


def test_registry_store_skips_the_spec_less_aggregate() -> None:
    # A dict-spec surface stays registry-distributed; the aggregate is skipped, not crashed.
    jito_spec = json.loads(open(JITO, encoding="utf-8").read())
    store = serve_mcp._registry_store(
        [("jito", jito_spec), ("paysh", _catalog_surface())]
    )
    names = set(store.names())
    assert "jito" in names
    assert "paysh" not in names  # no single spec -> not registry-distributed
