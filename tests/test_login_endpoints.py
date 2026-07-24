"""`/auth/login/*` endpoints + the GeckoKeyResolver gate — offline (ASGI, no socket, no Mongo).

The wire contract, falsified end-to-end through the real multi-surface app (behind its WAF):
start → {login_id}; verify → {api_key} once; wrong code → 401; rate limit → 429; disabled
(no service) → 503; and a minted key gates the served MCP mount (enabled → 200, disabled →
403). The Privy client + key registry are injected fakes, so nothing touches the network.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")  # the serve extra provides starlette + mcp

import anyio  # noqa: E402
import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from gecko.authlogin import LoginService, RateLimiter  # noqa: E402
from gecko.http_server import (  # noqa: E402
    MCP_PATH,
    build_http_app,
    build_multi_surface_app,
)
from gecko.keyauth import KeyGate  # noqa: E402
from gecko.keyregistry import (  # noqa: E402
    GeckoKeyResolver,
    InMemoryKeyRegistry,
    RegistryAllowlist,
    hash_key,
)
from gecko.privy_server import PrivyIdentity, PrivyServerError  # noqa: E402

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")
SUBJECT = "did:privy:endpoint-dev"
# account_id prefers the verified email (the fake returns email="dev@example.com").
ACCOUNT = "dev@example.com"
CODE = "123456"


class _FakePrivy:
    def __init__(self, *, good_code: str = CODE) -> None:
        self._good = good_code
        self._handles = (f"login-{n}" for n in itertools.count())

    def start(self, email: str) -> str:
        return next(self._handles)

    def verify(self, login_id: str, code: str) -> PrivyIdentity:
        if code != self._good:
            raise PrivyServerError("bad code")
        return PrivyIdentity(subject=SUBJECT, email="dev@example.com")


def _app(service: LoginService | None) -> Any:
    return build_multi_surface_app(
        [("pegana", PEGANA)], allowed_hosts=["testserver"], login_service=service
    )


def _service(**kwargs) -> tuple[LoginService, InMemoryKeyRegistry]:
    registry = InMemoryKeyRegistry()
    svc = LoginService(privy=_FakePrivy(), registry=registry, **kwargs)
    return svc, registry


# --- happy path: start -> verify -> minted key --------------------------------


def test_start_then_verify_returns_key_once_and_stores_hash_only():
    svc, registry = _service()
    with TestClient(_app(svc)) as client:
        r1 = client.post("/auth/login/start", json={"email": "dev@example.com"})
        assert r1.status_code == 200
        login_id = r1.json()["login_id"]

        r2 = client.post(
            "/auth/login/verify", json={"login_id": login_id, "code": CODE}
        )
        assert r2.status_code == 200
        api_key = r2.json()["api_key"]

    # The key maps to the Privy subject but lands DISABLED (login = identity, not
    # access), so the enable-gated resolver only returns it once the founder enables.
    assert GeckoKeyResolver(registry)(api_key) is None
    registry.set_account_enabled(ACCOUNT, True)
    assert GeckoKeyResolver(registry)(api_key) == ACCOUNT
    assert hash_key(api_key) in registry._by_hash
    assert api_key not in repr(registry._by_hash)


# --- rejections + disabled ----------------------------------------------------


def test_wrong_code_is_401_and_echoes_no_token():
    svc, registry = _service()
    with TestClient(_app(svc)) as client:
        login_id = client.post(
            "/auth/login/start", json={"email": "dev@example.com"}
        ).json()["login_id"]
        r = client.post(
            "/auth/login/verify", json={"login_id": login_id, "code": "SECRET-000000"}
        )
    assert r.status_code == 401
    assert "SECRET-000000" not in r.text
    assert registry._by_hash == {}


def test_rate_limit_trips_after_n_bad_codes():
    svc, _registry = _service(verify_limiter=RateLimiter(2, 3600))
    with TestClient(_app(svc)) as client:
        login_id = client.post(
            "/auth/login/start", json={"email": "dev@example.com"}
        ).json()["login_id"]
        codes = ["000000", "111111"]
        for c in codes:
            assert (
                client.post(
                    "/auth/login/verify", json={"login_id": login_id, "code": c}
                ).status_code
                == 401
            )
        r = client.post(
            "/auth/login/verify", json={"login_id": login_id, "code": "222222"}
        )
    assert r.status_code == 429


def test_bad_email_is_400():
    svc, _registry = _service()
    with TestClient(_app(svc)) as client:
        r = client.post("/auth/login/start", json={"email": "nope"})
    assert r.status_code == 400


def test_endpoints_503_when_login_disabled(monkeypatch):
    # No injected service and no env config -> the endpoints fail closed (503), not 500.
    monkeypatch.delenv("PRIVY_APP_SECRET", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with TestClient(_app(None)) as client:
        assert (
            client.post("/auth/login/start", json={"email": "d@e.com"}).status_code
            == 503
        )
        assert (
            client.post(
                "/auth/login/verify", json={"login_id": "x", "code": "y"}
            ).status_code
            == 503
        )


def test_oversized_body_is_400_not_500():
    svc, _registry = _service()
    with TestClient(_app(svc)) as client:
        r = client.post("/auth/login/start", content=b"{" + b"a" * 5000)
    assert r.status_code == 400


# --- the minted key gates the served MCP mount (GeckoKeyResolver) -------------

BASE = "http://test"
ALLOWED_HOST = "test"


def _gate(registry: InMemoryKeyRegistry) -> KeyGate:
    return KeyGate(
        resolve_account=GeckoKeyResolver(registry),
        allowlist=RegistryAllowlist(registry),
    )


def _gated_app(registry: InMemoryKeyRegistry) -> Any:
    return build_http_app(
        PEGANA,
        mode="recorded",
        allowed_hosts=[ALLOWED_HOST],
        allowed_origins=[BASE],
        gate=_gate(registry),
    )


def _init_post(app: Any, api_key: str) -> httpx.Response:
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
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {api_key}",
    }

    async def go() -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as client:
                return await client.post(MCP_PATH, json=init, headers=headers)

    return anyio.run(go)


def _mint(registry: InMemoryKeyRegistry) -> str:
    svc = LoginService(privy=_FakePrivy(), registry=registry)
    return svc.verify(svc.start("dev@example.com", "ip"), CODE, "ip")


def test_gate_with_enabled_gecko_key_passes_through():
    registry = InMemoryKeyRegistry()
    key = _mint(registry)
    # A login-minted key is IDENTITY ONLY — the founder must enable the account and
    # grant it this surface before it opens anything.
    registry.set_account_enabled(ACCOUNT, True)
    resp = _init_post(_gated_app(registry), key)
    assert resp.status_code == 200  # reached the transport (real init handshake)


def test_a_freshly_logged_in_key_opens_nothing_until_the_founder_grants_it():
    """Self-service login must not hand out access to a gated/paid surface."""
    registry = InMemoryKeyRegistry()
    key = _mint(registry)
    resp = _init_post(_gated_app(registry), key)
    assert resp.status_code == 403
    assert json.loads(resp.text)["reason"] == "invalid_token"  # disabled -> no account


def test_gate_with_disabled_gecko_key_is_403():
    registry = InMemoryKeyRegistry()
    key = _mint(registry)
    registry.set_account_enabled(ACCOUNT, False)
    resp = _init_post(_gated_app(registry), key)
    assert resp.status_code == 403
    body = json.loads(resp.text)
    assert body["reason"] == "invalid_token"  # disabled -> resolver returns None
    assert key not in resp.text  # the key is never echoed
