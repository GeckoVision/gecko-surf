"""Task 1: the canonical JSON-RPC transport seam (gecko/rpc.py).

Offline, no network: ``validate_rpc_url`` is a pure scheme check, and
``default_rpc_call``'s JSON-RPC error handling is proven with an injected fake HTTP
layer. Also asserts the layering fix — ``pda_testkit`` and ``pda_resolve`` import the
transport from ``gecko.rpc`` and keep working.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.rpc import (
    LOCAL_RPC,
    RpcError,
    default_rpc_call,
    validate_rpc_url,
)


def test_validate_accepts_loopback_and_public_https() -> None:
    # loopback (surfpool fork) AND public mainnet are both deliberately allowed
    validate_rpc_url(LOCAL_RPC)
    validate_rpc_url("http://127.0.0.1:8899")
    validate_rpc_url("https://api.mainnet-beta.solana.com")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "ws://y", ""])
def test_validate_rejects_non_http_scheme(url: str) -> None:
    with pytest.raises(RpcError):
        validate_rpc_url(url)


def test_default_rpc_call_passes_result_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, body: bytes) -> dict[str, Any]:
        captured["url"] = url
        return {"jsonrpc": "2.0", "id": 1, "result": {"value": {"lamports": 42}}}

    monkeypatch.setattr("gecko.rpc._http_post_json", fake_post)
    resp = default_rpc_call(LOCAL_RPC, "getAccountInfo", ["addr"])
    assert resp["result"]["value"]["lamports"] == 42
    assert captured["url"] == LOCAL_RPC


def test_default_rpc_call_raises_on_jsonrpc_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, body: bytes) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32602, "message": "Invalid params"},
        }

    monkeypatch.setattr("gecko.rpc._http_post_json", fake_post)
    with pytest.raises(RpcError) as exc:
        default_rpc_call(LOCAL_RPC, "simulateTransaction", ["secret-tx-body"])
    msg = str(exc.value)
    # method + code/message surface; the params body (which could be large) never does
    assert "simulateTransaction" in msg
    assert "-32602" in msg
    assert "Invalid params" in msg
    assert "secret-tx-body" not in msg


def test_default_rpc_call_validates_scheme_first() -> None:
    with pytest.raises(RpcError):
        default_rpc_call("file:///etc/passwd", "getHealth", [])


def test_http_post_sends_an_identifiable_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A CDN bot-check (Cloudflare error 1010) bans the default Python-urllib UA with a 403
    # before the request reaches the API. The transport must send a real gecko-surf UA.
    import gecko.rpc as rpc

    captured: dict[str, Any] = {}

    class FakeResp:
        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(req: Any, timeout: int = 0) -> FakeResp:
        captured["ua"] = req.get_header("User-agent")
        return FakeResp()

    monkeypatch.setattr(rpc.urllib.request, "urlopen", fake_urlopen)
    rpc._http_post_json("https://example.com", b"{}")
    assert captured["ua"] and captured["ua"].startswith("gecko-surf")


def test_layering_pda_modules_import_transport_from_rpc() -> None:
    # the bug fix: pda_resolve must source the transport from gecko.rpc, not pda_testkit
    import gecko.pda_resolve as pda_resolve
    import gecko.pda_testkit as pda_testkit
    from gecko.rpc import default_rpc_call as canonical

    # back-compat: pda_testkit still exposes the old private name, now aliased to canonical
    assert pda_testkit._default_rpc_call is canonical
    assert pda_resolve._default_rpc_call is canonical
