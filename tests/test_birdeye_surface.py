"""The hosted Birdeye surface — a PAID, key-gated Solana/DeFi data API served RECORDED.

Why recorded (same reasoning as TxLINE): serving a paid key-gated API live on a public
endpoint would spend the key's quota on anonymous traffic. Recorded synthesizes every
response from Birdeye's own schema ($0, no credential used or exposed) while all ops stay
visible and first-call-correct — an agent discovers and correctly forms the call here, then
runs it live with its OWN key.

Also pins the anti-poisoning regression: Birdeye's spec documents real base58 mints in
request `default`s and says "outgoing transfers" in a param description. Both used to
false-positive and wholesale-quarantine the surface (disabling auth). It must comprehend
CLEAN. Offline, $0.
"""

from __future__ import annotations

import gecko.serve_mcp as serve_mcp
from gecko.access import stub_session
from gecko.client import AgentApiClient


def _client() -> AgentApiClient:
    return AgentApiClient(str(serve_mcp._BIRDEYE_SPEC), session=stub_session())


def test_spec_ships_in_image() -> None:
    assert serve_mcp._BIRDEYE_SPEC.exists(), "birdeye spec must ship in-image"


def test_comprehends_the_full_surface() -> None:
    client = _client()
    # 88 paths -> ~89 ops; every op becomes a tool.
    assert len(client.operations) >= 88
    assert len(client.tools) == len(client.operations)


def test_surface_is_not_quarantined_anti_poison_regression() -> None:
    """The FP that blocked Birdeye: base58 mints in request `default`s + a benign
    "outgoing transfers" description must NOT flag. Zero poisoned tools."""
    client = _client()
    flagged = [t["name"] for t in client.tools if t.get("x-poison-flag")]
    assert flagged == [], f"anti-poisoning false positive returned: {flagged}"


def test_auth_is_hidden_from_the_agent() -> None:
    """X-API-KEY is the security scheme — injected at call time, never an agent param."""
    client = _client()
    for tool in client.tools:
        props = (tool.get("inputSchema") or {}).get("properties", {}) or {}
        assert not any(
            "api-key" in p.lower() or "apikey" in p.lower() for p in props
        ), f"{tool['name']} exposes the API key to the agent"


def test_recorded_call_is_first_call_correct_with_no_key() -> None:
    """A $0 recorded call forms the real Birdeye URL — no credential needed."""
    client = _client()
    price = next(t["name"] for t in client.tools if t["name"] == "get-defi-price")
    result = client.call(
        price,
        {"address": "So11111111111111111111111111111111111111112"},
        mode="recorded",
    )
    assert result["status"] == 200
    assert "public-api.birdeye.so/defi/price" in result["request"]
    assert "address=So1111" in result["request"]


def test_hosted_surface_is_served_recorded() -> None:
    """The hosted entry must be RECORDED — never live on a public endpoint (paid key)."""
    surfaces = dict(serve_mcp._build_surfaces(hosted_enforce="block"))
    birdeye = surfaces["birdeye"]
    assert birdeye.mode == "recorded"
