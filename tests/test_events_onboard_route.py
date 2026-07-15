"""POST /events/onboard — the hosted ingest for the `gecko add` onboard ping.

Server-side contract (mirrors the /comprehend front-door tests):

  * a valid body → 204 (empty) + ONE ``surf.onboard`` event to the sink, with
    ``surface_id`` reduced through the events module's opaque-token path;
  * ANY invalid body — junk JSON, unknown/missing keys, an oversized body, a >64-char
    value, a mode outside recorded|live, a non-string value — → the SAME 204 and
    NOTHING emitted (a scraper never learns which probe shape was closer);
  * a value that passes the wire caps but is secret-shaped fails closed inside the
    events module → still 204, still nothing emitted, NEVER a 500.

Offline: the fake sink is injected via ``set_surf_sink_override``; no Mongo, no wire.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko import events  # noqa: E402
from gecko.http_server import (  # noqa: E402
    EVENTS_ONBOARD_PATH,
    MAX_ONBOARD_PING_BYTES,
    build_multi_surface_app,
    parse_onboard_ping,
)

PEGANA = "tests/fixtures/pegana_openapi.json"

VALID = {
    "surface_host": "api.stripe.com",
    "version": "0.4.4",
    "client_os": "linux",
    "install_id": "0f" * 16,  # uuid4-hex shaped
    "mode": "recorded",
}


@pytest.fixture()
def sink(monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    docs: list[dict[str, Any]] = []
    events.set_surf_sink_override(lambda d: docs.append(dict(d)))
    yield docs
    events.set_surf_sink_override(None)


def _app() -> Any:
    return build_multi_surface_app([("pegana", PEGANA)], allowed_hosts=["testserver"])


# --- the happy path -------------------------------------------------------- #


def test_valid_ping_returns_204_and_emits_one_event(sink) -> None:
    with TestClient(_app()) as c:
        r = c.post(EVENTS_ONBOARD_PATH, json=VALID)
        assert r.status_code == 204
        assert r.content == b""  # nothing useful to a scraper, ever
    assert len(sink) == 1
    doc = sink[0]
    assert doc["event"] == "surf.onboard"
    assert doc["surface_id"] == "api.stripe.com"
    assert doc["version"] == "0.4.4"
    assert doc["client_os"] == "linux"
    assert doc["install_id"] == "0f" * 16
    assert doc["mode"] == "recorded"
    assert set(doc) <= events.RECORD_ALLOWED_KEYS


def test_url_shaped_surface_host_is_reduced_to_bare_host(sink) -> None:
    # A full URL-with-creds that fits the 64-char cap still loses scheme, userinfo,
    # path — the events module's existing opaque-token reduction runs on the way in.
    body = dict(VALID, surface_host="https://bob:pw@api.x.example/v1?t=S")
    with TestClient(_app()) as c:
        assert c.post(EVENTS_ONBOARD_PATH, json=body).status_code == 204
    assert sink[0]["surface_id"] == "api.x.example"
    import json as _json

    raw = _json.dumps(sink)
    assert "pw" not in raw.split("api.x.example")[0]  # no userinfo survived
    assert "/v1" not in raw and "t=S" not in raw


# --- every rejection: same 204, nothing emitted ----------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        dict(VALID, extra="key"),  # unknown key
        {k: v for k, v in VALID.items() if k != "install_id"},  # missing key
        dict(VALID, mode="probe"),  # a CallMode member the ping set still rejects
        dict(VALID, mode="banana"),  # free-text mode
        dict(VALID, version="x" * 65),  # value over the 64-char cap
        dict(VALID, install_id=123),  # non-string value
        dict(VALID, surface_host=""),  # empty value
        ["not", "an", "object"],  # non-dict JSON
    ],
)
def test_invalid_body_returns_204_and_emits_nothing(sink, body) -> None:
    with TestClient(_app()) as c:
        r = c.post(EVENTS_ONBOARD_PATH, json=body)
        assert r.status_code == 204
        assert r.content == b""
    assert sink == []


def test_junk_bytes_return_204_and_emit_nothing(sink) -> None:
    with TestClient(_app()) as c:
        r = c.post(
            EVENTS_ONBOARD_PATH,
            content=b"\xff\xfenot json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 204
    assert sink == []


def test_oversized_body_returns_204_and_emits_nothing(sink) -> None:
    pad = "x" * (MAX_ONBOARD_PING_BYTES + 1)
    with TestClient(_app()) as c:
        r = c.post(EVENTS_ONBOARD_PATH, json=dict(VALID, pad=pad))
        assert r.status_code == 204
    assert sink == []


def test_secret_shaped_value_fails_closed_inside_events_never_500s(sink) -> None:
    # Passes the wire caps (<=64 chars) but trips the events module's secret-shape
    # gate — the route must swallow the fail-closed TelemetryError: 204, no emit.
    body = dict(VALID, version="sk-" + "a" * 30)
    with TestClient(_app()) as c:
        r = c.post(EVENTS_ONBOARD_PATH, json=body)
        assert r.status_code == 204
    assert sink == []


def test_get_is_not_served(sink) -> None:
    with TestClient(_app()) as c:
        assert c.get(EVENTS_ONBOARD_PATH).status_code == 405
    assert sink == []


# --- the strict parser directly -------------------------------------------- #


def test_parse_onboard_ping_caps_raw_body_length() -> None:
    import json as _json

    raw = _json.dumps(VALID).encode()
    assert parse_onboard_ping(raw) == VALID
    assert parse_onboard_ping(b"x" * (MAX_ONBOARD_PING_BYTES + 1)) is None
