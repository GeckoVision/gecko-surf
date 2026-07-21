"""``GECKO_GATED_SURFACES`` misconfiguration must never silently open the PAID surface.

Red-team finding (adversarial review of the per-surface gate): the gate is the ONLY
thing between the public internet and a paid third-party surface, and its *scope* comes
from a free-text env var. Three plausible operator mistakes silently produced a paid
surface with NO gate at all — ``GECKO_REQUIRE_KEY=1`` on, the gate wired, and every
mount open, with no error anywhere:

* ``GECKO_GATED_SURFACES=","`` (or ``",,,"``) — non-empty, so it did NOT fall back to the
  hosted default, and parsed to the empty set ⇒ **nothing gated**.
* ``GECKO_GATED_SURFACES=BIRDEYE`` — a casing slip against a lowercase mount name.
* ``GECKO_GATED_SURFACES=birdye`` — a typo naming no served surface (stays inert *by
  design*, but must be LOUD, not silent).

Fail-closed direction only: every assertion here demands MORE gating or a louder signal,
never less. Fully offline (in-process ASGI + the in-memory key registry fake).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import (  # noqa: E402
    GATED_SURFACES_ENV,
    build_multi_surface_app,
    resolve_gated_surfaces,
)
from gecko.keyregistry import InMemoryKeyRegistry, hash_key, mint_key  # noqa: E402

SPEC = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")
PAID = "birdeye"
OPEN_SURFACE = "jupiter"
DEFAULT = frozenset({PAID})

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        GATED_SURFACES_ENV,
        "GECKO_REQUIRE_KEY",
        "MONGODB_URI",
        "PRIVY_APP_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _registry() -> InMemoryKeyRegistry:
    registry = InMemoryKeyRegistry()
    registry.store_key(key_hash=hash_key(mint_key()), account_id="dev", label="l")
    return registry


def _app(gated: Any):
    return build_multi_surface_app(
        [(PAID, SPEC), (OPEN_SURFACE, SPEC)],
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=gated,
        key_registry=_registry(),
    )


def _status(gated: Any, surface: str) -> int:
    with TestClient(_app(gated)) as client:
        return client.post(f"/{surface}/mcp", json=_INIT, headers=_HEADERS).status_code


# --- a garbage env value must fall back to the default, never to "gate nothing" ---


@pytest.mark.parametrize("raw", [",", ",,,", " , , "])
def test_env_that_parses_to_no_names_falls_back_to_the_default(monkeypatch, raw):
    """A non-empty value that yields zero names used to gate NOTHING. Fail closed:
    fall back to the hosted default (which can only ever gate more, never less)."""
    monkeypatch.setenv(GATED_SURFACES_ENV, raw)
    assert resolve_gated_surfaces(default=DEFAULT) == DEFAULT


def test_env_that_parses_to_no_names_is_reported(monkeypatch, caplog):
    monkeypatch.setenv(GATED_SURFACES_ENV, ",,")
    with caplog.at_level(logging.ERROR, logger="gecko.http_server"):
        resolve_gated_surfaces(default=DEFAULT)
    assert any(GATED_SURFACES_ENV in r.message for r in caplog.records)


# --- a casing slip must still gate the paid mount --------------------------------


@pytest.mark.parametrize("named", ["BIRDEYE", "Birdeye", "BirdEye"])
def test_casing_slip_still_gates_the_paid_surface(named):
    """Mount names are lowercase; matching case-insensitively can only ever gate MORE."""
    assert _status(frozenset({named}), PAID) == 403


def test_casing_slip_still_leaves_the_funnel_open():
    assert _status(frozenset({"BIRDEYE"}), OPEN_SURFACE) == 200


# --- a typo names no served surface: inert by design, but must be LOUD -----------


def test_a_gated_name_this_host_does_not_serve_is_logged_as_an_error(caplog):
    with caplog.at_level(logging.ERROR, logger="gecko.http_server"):
        _app(frozenset({"birdye"}))
    assert "birdye" in " ".join(r.getMessage() for r in caplog.records)


def test_a_gate_that_gates_no_served_surface_is_logged_as_an_error(caplog):
    """The dangerous end state: the gate is ON and every mount is OPEN."""
    with caplog.at_level(logging.ERROR, logger="gecko.http_server"):
        _app(frozenset({"birdye"}))
    assert any("gates NO served surface" in str(r.msg) for r in caplog.records)


def test_a_correct_config_logs_no_error(caplog):
    with caplog.at_level(logging.ERROR, logger="gecko.http_server"):
        _app(frozenset({PAID}))
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


# --- nothing above may weaken the gate itself ------------------------------------


def test_the_paid_surface_is_still_denied_and_the_funnel_still_open():
    assert _status(frozenset({PAID}), PAID) == 403
    assert _status(frozenset({PAID}), OPEN_SURFACE) == 200


def test_gate_all_default_still_gates_every_mount():
    assert _status(None, PAID) == 403
    assert _status(None, OPEN_SURFACE) == 403
