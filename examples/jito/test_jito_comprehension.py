"""Gecko comprehends the Jito Block Engine.

The Jito Block Engine (https://docs.jito.wtf, by Jito Labs) is the JSON-RPC
service Solana searchers/dApps use to submit MEV bundles and low-latency
transactions. It has NO OpenAPI: its surface lives in a JS-driven docs site
(docs.jito.wtf) plus the official SDK source. This example is the
docs->draft-OpenAPI on-ramp — we authored `spec/jito_blockengine_openapi.json`
from the rendered docs page and cross-checked it against jito-labs SDKs, then
let the unmodified Gecko engine comprehend it.

These tests prove the comprehension end-to-end, engine-only, $0, offline (no
network, no anthropic): intent -> the right JSON-RPC method, a recorded call that
comes back as a well-formed JSON-RPC envelope, and the first-call-correct guard
that catches a missing required param instead of firing a malformed bundle.

Run: uv run pytest examples/jito/ -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko import AgentApiClient, public_session
from gecko.caller import CallError

SPEC = Path(__file__).parent / "spec" / "jito_blockengine_openapi.json"


def _client() -> AgentApiClient:
    # Default sends need no auth key; the public (no-auth) session adapter.
    return AgentApiClient(str(SPEC), session=public_session())


def test_full_surface_comprehended() -> None:
    client = _client()
    names = {t["name"] for t in client.list_tools()}
    # The five JSON-RPC methods + the one REST tip-floor read we modeled.
    assert names == {
        "sendBundle",
        "getTipAccounts",
        "getBundleStatuses",
        "getInflightBundleStatuses",
        "sendTransaction",
        "getTipFloor",
    }


def test_search_submit_bundle_finds_send_bundle() -> None:
    client = _client()
    hits = [h["name"] for h in client.search("submit a bundle of transactions")]
    assert hits, "expected a discovery hit"
    assert hits[0] == "sendBundle"


def test_search_tip_accounts_finds_get_tip_accounts() -> None:
    client = _client()
    hits = [h["name"] for h in client.search("get the tip accounts")]
    assert hits[0] == "getTipAccounts"


def test_search_inflight_finds_inflight_statuses() -> None:
    client = _client()
    hits = [h["name"] for h in client.search("status of in-flight bundles")]
    assert hits[0] == "getInflightBundleStatuses"


def test_search_single_transaction_finds_send_transaction() -> None:
    client = _client()
    hits = [h["name"] for h in client.search("send a single transaction fast")]
    assert hits[0] == "sendTransaction"


def test_search_recent_tips_finds_tip_floor() -> None:
    client = _client()
    hits = [h["name"] for h in client.search("recent tip amounts")]
    assert hits[0] == "getTipFloor"


def test_recorded_send_bundle_returns_jsonrpc_envelope() -> None:
    client = _client()
    result = client.call(
        "sendBundle",
        {"body": {"transactions": ["<base64-signed-tx>"], "encoding": "base64"}},
        mode="recorded",
    )
    assert result["status"] == 200
    assert result["method"] == "POST"
    # JSON-RPC 2.0 envelope synthesized from the response schema: a bundle_id.
    assert isinstance(result["data"], dict)
    assert result["data"]["jsonrpc"] == "2.0"
    assert isinstance(result["data"]["result"], str)
    assert result["data"]["result"]


def test_recorded_get_tip_accounts_returns_eight_accounts() -> None:
    client = _client()
    result = client.call("getTipAccounts", {}, mode="recorded")
    assert result["status"] == 200
    accounts = result["data"]["result"]
    assert isinstance(accounts, list)
    # Jito documents a constant set of 8 tip accounts.
    assert len(accounts) == 8
    assert all(isinstance(a, str) for a in accounts)


def test_missing_required_transactions_is_caught_not_fired() -> None:
    client = _client()
    # Drop the required `transactions` from sendBundle: the caller must catch the
    # malformed JSON-RPC call instead of firing an empty bundle at the engine.
    with pytest.raises(CallError) as exc:
        client.call(
            "sendBundle",
            {"body": {"encoding": "base64"}},
            mode="recorded",
        )
    assert "transactions" in str(exc.value)


def test_default_sends_need_no_auth() -> None:
    # Docs: "you no longer need an approved auth key for default sends." The
    # optional UUID scheme must NOT gate any surfaced tool.
    client = _client()
    assert all(not t.get("requires_auth") for t in client.list_tools())
