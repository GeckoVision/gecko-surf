"""Registry routes: anon free fetch, 402 premium gate, OTP endpoints."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gecko.registry.api import registry_routes
from gecko.registry.keys import KeyStore
from gecko.registry.store import RegistrySurface, SurfaceStore
from tests.test_registry_keys import Clock, FakeCollection


@pytest.fixture(autouse=True)
def _clean_ip_throttle():
    from gecko.registry import api as _api

    _api._ip_counts.clear()
    yield
    _api._ip_counts.clear()


SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "T", "version": "1"},
    "paths": {
        "/x": {
            "get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}
        }
    },
}


def _client() -> tuple[TestClient, KeyStore, list[tuple[str, str]]]:
    store = SurfaceStore(
        [
            RegistrySurface(name="colosseum", spec=SPEC, tier="free"),
            RegistrySurface(name="txline", spec=SPEC, tier="premium"),
        ]
    )
    sent: list[tuple[str, str]] = []
    keys = KeyStore(
        keys_collection=FakeCollection(),
        otp_collection=FakeCollection(),
        mailer=lambda e, c: sent.append((e, c)),
        clock=Clock(),
    )
    app = Starlette(routes=registry_routes(store, keys))
    return TestClient(app), keys, sent


def test_list_surfaces_anon():
    client, _, _ = _client()
    r = client.get("/registry/surfaces")
    assert r.status_code == 200
    names = {s["name"]: s for s in r.json()["surfaces"]}
    assert names["colosseum"]["tier"] == "free"
    assert "spec" not in names["colosseum"]  # list is light; fetch gets the spec


def test_fetch_free_surface_anon():
    client, _, _ = _client()
    r = client.get("/registry/surfaces/colosseum")
    assert r.status_code == 200
    body = r.json()
    assert body["spec"] == SPEC and body["surface_rev"]


def test_fetch_premium_without_key_is_402():
    client, _, _ = _client()
    r = client.get("/registry/surfaces/txline")
    assert r.status_code == 402
    assert r.json()["error"] == "entitlement_required"


def test_fetch_premium_with_entitled_key():
    client, keys, sent = _client()
    client.post("/registry/keys", json={"email": "dev@example.com"})
    plain = keys.verify_otp("dev@example.com", sent[0][1])
    # grant flat per-surface entitlement directly (founder-run at v1)
    for d in keys._keys.docs:
        d["surfaces"] = ["txline"]
    r = client.get("/registry/surfaces/txline", headers={"X-Gecko-Key": plain})
    assert r.status_code == 200


def test_unknown_surface_404():
    client, _, _ = _client()
    assert client.get("/registry/surfaces/nope").status_code == 404


def test_otp_endpoints_roundtrip():
    client, _, sent = _client()
    r = client.post("/registry/keys", json={"email": "dev@example.com"})
    assert r.status_code == 202
    code = sent[0][1]
    r = client.post(
        "/registry/keys/verify", json={"email": "dev@example.com", "otp": code}
    )
    assert r.status_code == 200
    assert r.json()["key"].startswith("gk_live_")
    # wrong otp -> 401, no key material in the body
    r = client.post(
        "/registry/keys/verify", json={"email": "dev@example.com", "otp": "000000"}
    )
    assert r.status_code == 401
    assert "gk_live_" not in r.text


def test_keys_endpoint_rejects_oversized_body():
    client, _, _ = _client()
    r = client.post(
        "/registry/keys",
        content=b"x" * 5000,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_reserved_surface_name_registry_rejected():
    import pytest as _pytest

    from gecko.http_server import build_multi_surface_app
    from gecko.registry.api import registry_routes as rr
    from gecko.registry.store import RegistrySurface, SurfaceStore

    store = SurfaceStore([RegistrySurface(name="colosseum", spec=SPEC, tier="free")])
    with _pytest.raises(ValueError, match="reserved"):
        build_multi_surface_app([("registry", SPEC)], registry_routes=rr(store, None))


def test_per_ip_throttle_still_202_but_stops_sending():
    client, _, sent = _client()
    for i in range(15):
        r = client.post("/registry/keys", json={"email": f"a{i}@example.com"})
        assert r.status_code == 202
    assert len(sent) <= 10  # throttled sends stop, response shape unchanged
