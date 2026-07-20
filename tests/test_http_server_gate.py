"""Layer 1 gate over the served MCP mount — offline (httpx ASGITransport, no socket).

Two things matter here:
1. **Gate OFF is byte-identical to today** — the critical regression. With no gate
   wired, the ``/mcp`` route object is the SAME app instance as before, and a real MCP
   handshake + call still round-trips first-call-correct.
2. **Gate ON** — an unauthorized/invalid/absent key gets a clean 403 (no token echoed);
   an enabled Gecko key passes straight through to the existing handler (200).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

mcp = pytest.importorskip("mcp")  # skip cleanly if the serve extra isn't installed

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko.http_server import (  # noqa: E402
    MCP_PATH,
    build_http_app,
    resolve_require_gecko_key,
)
from gecko.keyauth import KeyGate  # noqa: E402

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")
BASE = "http://test"
ALLOWED_HOST = "test"

TOKEN = "eyJ-SECRET-gecko-key-DO-NOT-LEAK.aaa.bbb"
ACCOUNT = "did:privy:enabled-dev"


def _app(gate: KeyGate | None = None) -> Any:
    return build_http_app(
        PEGANA,
        mode="recorded",
        allowed_hosts=[ALLOWED_HOST],
        allowed_origins=[BASE],
        gate=gate,
    )


class _SetAllowlist:
    def __init__(self, enabled: set[str]) -> None:
        self._enabled = enabled

    def is_enabled(self, account: str) -> bool:
        return account in self._enabled


def _gate(enabled: set[str]) -> KeyGate:
    def resolve(token: str) -> str | None:
        return ACCOUNT if token == TOKEN else None

    return KeyGate(resolve_account=resolve, allowlist=_SetAllowlist(enabled))


# --- byte-identical when OFF (the regression) --------------------------------


def test_gate_off_leaves_mcp_route_object_untouched():
    # The default (no gate) must not wrap the /mcp mount at all: same app instance.
    plain = _app()
    plain_mcp = next(r for r in plain.routes if getattr(r, "path", None) == MCP_PATH)
    # The endpoint is the raw capture-ASGI app (its class name, unwrapped by any gate).
    assert type(plain_mcp.app).__name__ == "_InitializeCaptureASGI"


def test_gate_off_default_env_is_off(monkeypatch):
    monkeypatch.delenv("GECKO_REQUIRE_KEY", raising=False)
    assert resolve_require_gecko_key() is False
    monkeypatch.setenv("GECKO_REQUIRE_KEY", "1")
    assert resolve_require_gecko_key() is True
    # explicit always wins over env
    assert resolve_require_gecko_key(False) is False


def _call(
    app: Any, name: str, args: dict[str, Any], headers: dict[str, str] | None = None
):
    async def body(session: ClientSession) -> str:
        res = await session.call_tool(name, args)
        return res.content[0].text  # type: ignore[union-attr]

    async def go() -> str:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=BASE,
                headers=headers or {},
            ) as http_client:
                async with streamable_http_client(
                    f"{BASE}/mcp", http_client=http_client
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await body(session)

    return anyio.run(go)


def test_gate_off_full_handshake_still_first_call_correct():
    raw = _call(_app(), "state", {"symbol": "USDC"})
    result = json.loads(raw)
    assert result["status"] == 200
    assert result["mode"] == "recorded"
    assert result["request"].endswith("/v1/assets/USDC/state")


# --- gate ON: 403 unauthorized, 200 passthrough ------------------------------


def _post_mcp(app: Any, headers: dict[str, str]) -> httpx.Response:
    """A raw initialize POST to /mcp so we can read the HTTP status directly."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **headers,
    }

    async def go() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                return await client.post(MCP_PATH, json=init, headers=req_headers)

    return anyio.run(go)


def test_gate_on_missing_key_is_403():
    resp = _post_mcp(_app(_gate({ACCOUNT})), headers={})
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "missing_token"
    assert TOKEN not in resp.text  # token never echoed


def test_gate_on_invalid_key_is_403():
    resp = _post_mcp(_app(_gate({ACCOUNT})), {"Authorization": "Bearer not-a-real-key"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "invalid_token"


def test_gate_on_valid_but_not_enabled_is_403():
    resp = _post_mcp(_app(_gate(set())), {"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_enabled"
    assert TOKEN not in resp.text


def test_gate_on_enabled_key_passes_through_first_call_correct():
    # The valid, enabled key reaches the SAME handler as the keyless surface.
    raw = _call(
        _app(_gate({ACCOUNT})),
        "state",
        {"symbol": "USDC"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    result = json.loads(raw)
    assert result["status"] == 200
    assert result["request"].endswith("/v1/assets/USDC/state")


def test_gate_on_denial_does_not_echo_token_in_any_form():
    resp = _post_mcp(_app(_gate(set())), {"Authorization": f"Bearer {TOKEN}"})
    assert TOKEN not in resp.text
    assert ACCOUNT not in resp.text  # not even the account leaks to the client


# --- gate ON with the REAL PrivyAccountResolver (end-to-end, offline JWT) -----
# A signed Privy-shaped JWT flows through the same ASGI gate. Offline: an ephemeral
# EC keypair signs the token and an injected key source verifies it (no JWKS network).

import time  # noqa: E402

from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from gecko.privy_auth import (  # noqa: E402
    PRIVY_JWT_ISSUER,
    PrivyAccountResolver,
)

PRIVY_APP_ID = "clzappid123"
PRIVY_SUBJECT = "did:privy:real-resolver-dev"


def _privy_jwt(private_key: Any, *, aud: str = PRIVY_APP_ID) -> str:
    jwt = pytest.importorskip("jwt")
    now = int(time.time())
    return jwt.encode(
        {
            "sub": PRIVY_SUBJECT,
            "aud": aud,
            "iss": PRIVY_JWT_ISSUER,
            "iat": now,
            "exp": now + 3600,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "kid-1"},
    )


def _privy_gate(enabled: set[str], public_key: Any) -> KeyGate:
    resolver = PrivyAccountResolver(
        app_id=PRIVY_APP_ID, key_source=lambda _kid: public_key
    )
    return KeyGate(resolve_account=resolver, allowlist=_SetAllowlist(enabled))


def test_privy_resolver_enabled_subject_passes_through_first_call_correct():
    key = ec.generate_private_key(ec.SECP256R1())
    token = _privy_jwt(key)
    raw = _call(
        _app(_privy_gate({PRIVY_SUBJECT}, key.public_key())),
        "state",
        {"symbol": "USDC"},
        headers={"Authorization": f"Bearer {token}"},
    )
    result = json.loads(raw)
    assert result["status"] == 200
    assert result["request"].endswith("/v1/assets/USDC/state")


def test_privy_resolver_valid_but_not_enabled_is_403():
    key = ec.generate_private_key(ec.SECP256R1())
    token = _privy_jwt(key)
    resp = _post_mcp(
        _app(_privy_gate(set(), key.public_key())),
        {"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "not_enabled"
    assert token not in resp.text  # the JWT is never echoed


def test_privy_resolver_wrong_audience_is_403_invalid():
    key = ec.generate_private_key(ec.SECP256R1())
    token = _privy_jwt(key, aud="some-other-app")  # audience mismatch -> unverifiable
    resp = _post_mcp(
        _app(_privy_gate({PRIVY_SUBJECT}, key.public_key())),
        {"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "invalid_token"


# --- the default when the gate is on but Privy is NOT configured: deny_all ----


def test_multi_surface_gate_on_without_privy_config_denies_everyone(monkeypatch):
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    monkeypatch.setenv("GECKO_REQUIRE_KEY", "1")
    monkeypatch.delenv("PRIVY_APP_ID", raising=False)  # no Privy config -> fail closed

    app = build_multi_surface_app(
        [("pegana", PEGANA)],
        allowed_hosts=["testserver"],
    )
    # Even a well-formed bearer is denied — deny_all resolves nothing (invalid_token).
    with TestClient(app) as client:
        resp = client.post(
            "/pegana/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer any-well-formed-looking-token",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "invalid_token"
