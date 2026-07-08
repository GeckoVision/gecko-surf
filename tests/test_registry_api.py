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


def test_feedback_accepts_closed_vocab_only(tmp_path):
    import json as _json

    from gecko.registry.api import registry_routes as rr
    from gecko.registry.store import RegistrySurface, SurfaceStore

    store = SurfaceStore([RegistrySurface(name="colosseum", spec=SPEC, tier="free")])
    log = tmp_path / "feedback.jsonl"
    app = Starlette(routes=rr(store, None, feedback_path=str(log)))
    client = TestClient(app)

    ok = client.post(
        "/registry/feedback",
        json={
            "surface": "colosseum",
            "surface_rev": "abc",
            "classes": ["call.upstream_schema_reject"],
        },
    )
    assert ok.status_code == 204
    line = _json.loads(log.read_text("utf-8").splitlines()[0])
    assert line["classes"] == ["call.upstream_schema_reject"]

    bad = client.post(
        "/registry/feedback",
        json={
            "surface": "colosseum",
            "surface_rev": "abc",
            "classes": ["lol.free_text"],
        },
    )
    assert bad.status_code == 400
    assert len(log.read_text("utf-8").splitlines()) == 1  # nothing appended


def test_feedback_throttled_per_ip_silent_no_write(tmp_path):
    import json as _json

    from gecko.registry.api import registry_routes as rr
    from gecko.registry.store import RegistrySurface, SurfaceStore

    store = SurfaceStore([RegistrySurface(name="colosseum", spec=SPEC, tier="free")])
    log = tmp_path / "feedback.jsonl"
    app = Starlette(routes=rr(store, None, feedback_path=str(log)))
    client = TestClient(app)

    for _ in range(15):
        r = client.post(
            "/registry/feedback",
            json={
                "surface": "colosseum",
                "surface_rev": "abc",
                "classes": ["call.upstream_schema_reject"],
            },
        )
        assert r.status_code == 204  # throttled breach stays silent, same 204 shape

    lines = log.read_text("utf-8").splitlines() if log.exists() else []
    assert len(lines) <= 10
    for line in lines:
        assert _json.loads(line)["classes"] == ["call.upstream_schema_reject"]


def test_search_across_surfaces():
    client, _, _ = _client()
    r = client.get("/registry/search", params={"intent": "get x"})
    assert r.status_code == 200
    surfaces = [x["surface"] for x in r.json()["results"]]
    assert "colosseum" in surfaces


def test_search_excludes_premium_unless_entitled():
    client, keys, sent = _client()

    r = client.get("/registry/search", params={"intent": "get x"})
    assert r.status_code == 200
    surfaces = [x["surface"] for x in r.json()["results"]]
    assert "colosseum" in surfaces
    assert "txline" not in surfaces  # premium, anonymous — not entitled

    client.post("/registry/keys", json={"email": "dev@example.com"})
    plain = keys.verify_otp("dev@example.com", sent[0][1])
    # grant flat per-surface entitlement directly (founder-run at v1)
    for d in keys._keys.docs:
        d["surfaces"] = ["txline"]

    r = client.get(
        "/registry/search",
        params={"intent": "get x"},
        headers={"X-Gecko-Key": plain},
    )
    assert r.status_code == 200
    surfaces = [x["surface"] for x in r.json()["results"]]
    assert "txline" in surfaces


def test_search_caches_client_per_surface(monkeypatch):
    """The cache in ``registry_routes`` must actually cache: a real cache builds
    each surface's ``AgentApiClient`` at most once no matter how many search
    requests come in. Spy on construction via a counting fake swapped in for
    the module-level ``AgentApiClient`` import."""
    import gecko.registry.api as _api

    calls = {"n": 0}

    class _CountingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls["n"] += 1

        def search(self, query: str, limit: int = 3) -> list[dict[str, str]]:
            return [{"name": "getX", "summary": "", "path": "/x", "method": "GET"}]

    monkeypatch.setattr(_api, "AgentApiClient", _CountingClient)

    client, _, _ = _client()
    # Anonymous: only the one free surface ("colosseum") is searchable — txline
    # is skipped by the entitlement gate before _make_client ever runs.
    client.get("/registry/search", params={"intent": "get x"})
    client.get("/registry/search", params={"intent": "get x"})
    assert calls["n"] == 1  # not 2 — the second request must hit the cache
