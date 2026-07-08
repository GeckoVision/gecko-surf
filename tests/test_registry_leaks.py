"""The 'never stored, never logged' promise is a test, not a sentence.

Sentinel key/OTP must appear in ZERO of: log records, error text, HTTP
responses (other than the one-time issuance), and the feedback log.
"""

import json
import logging

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gecko.registry.api import registry_routes
from gecko.registry.keys import KeyStore
from gecko.registry.store import RegistrySurface, SurfaceStore
from tests.test_registry_api import SPEC
from tests.test_registry_keys import Clock, FakeCollection


@pytest.fixture(autouse=True)
def _clean_ip_throttle():
    from gecko.registry import api as _api

    _api._ip_counts.clear()
    yield
    _api._ip_counts.clear()


def test_no_key_material_leaks(tmp_path, caplog):
    sent: list[tuple[str, str]] = []
    keys = KeyStore(
        keys_collection=FakeCollection(),
        otp_collection=FakeCollection(),
        mailer=lambda e, c: sent.append((e, c)),
        clock=Clock(),
    )
    store = SurfaceStore(
        [
            RegistrySurface(name="colosseum", spec=SPEC, tier="free"),
            RegistrySurface(name="txline", spec=SPEC, tier="premium"),
        ]
    )
    log = tmp_path / "fb.jsonl"
    app = Starlette(routes=registry_routes(store, keys, feedback_path=str(log)))
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG):
        client.post("/registry/keys", json={"email": "dev@example.com"})
        otp = sent[0][1]
        plain = client.post(
            "/registry/keys/verify", json={"email": "dev@example.com", "otp": otp}
        ).json()["key"]
        # Grant entitlement to the premium surface so the fetch path exercises
        # the successful retrieval, not a 402 error.
        for d in keys._keys.docs:
            d["surfaces"] = ["txline"]
        # exercise every route with the live key
        r1 = client.get("/registry/surfaces", headers={"X-Gecko-Key": plain})
        r2 = client.get("/registry/surfaces/txline", headers={"X-Gecko-Key": plain})
        r3 = client.get(
            "/registry/search", params={"intent": "x"}, headers={"X-Gecko-Key": plain}
        )
        client.post(
            "/registry/feedback",
            headers={"X-Gecko-Key": plain},
            json={
                "surface": "colosseum",
                "surface_rev": "r",
                "classes": ["call.upstream_schema_reject"],
            },
        )
        # Emit a benign log line to prove caplog is wired; this tripwire is
        # meaningful only if capture is working (so the leak assertions below fire).
        logging.getLogger("gecko.registry").warning("leak-suite marker")

    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    stored = json.dumps(keys._keys.docs) + json.dumps(keys._otps.docs)
    responses = r1.text + r2.text + r3.text
    fb = log.read_text("utf-8") if log.exists() else ""
    # Confirm caplog is wired — must see the benign marker.
    assert "leak-suite marker" in logged
    # The actual leak assertions are only meaningful if the above passes.
    for blob, where in (
        (logged, "logs"),
        (stored, "storage"),
        (responses, "responses"),
        (fb, "feedback log"),
    ):
        assert plain not in blob, f"key leaked into {where}"
        # OTP is now hashed at rest (code_hash + salt) — the plaintext code
        # must appear nowhere, including storage.
        assert otp not in blob, f"otp leaked into {where}"


def test_end_to_end_fetch_serve_prepare(tmp_path):
    """Registry -> runner cache -> AgentApiClient -> prepared request, offline."""
    from gecko.client import AgentApiClient
    from gecko.registry.client import fetch_surface

    store = SurfaceStore([RegistrySurface(name="colosseum", spec=SPEC, tier="free")])
    app = Starlette(routes=registry_routes(store, None))
    http = TestClient(app)

    def transport(url, headers):
        path = url.split("registry.example.com", 1)[1]
        r = http.get(path, headers=headers)
        return r.status_code, r.text

    fetched = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )

    # Inline session class respects the non-secret contract (auth_headers is
    # an adapter seam, not a secret store). This bearer token is test-only;
    # for live sessions, use Session or establish_session from gecko.access.
    class _BearerSession:
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer sk-local"}

    client = AgentApiClient(
        fetched.spec,
        base_url="https://api.example.com",
        session=_BearerSession(),
    )
    req = client.prepare("getX", {})
    assert req.url == "https://api.example.com/x"
    assert req.headers["Authorization"] == "Bearer sk-local"
