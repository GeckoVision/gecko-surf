"""Honeypot tripwire — decoy tools a comprehended surface NEVER emits.

A honeypot is a DETECTION tripwire, not a moat: it flags an agent that *probes*
(enumerates + calls a decoy) but does not stop a targeted first-shot attack, and it
is copyable. It is OFF by default — a real surface shows no fake tools unless the
operator opts in. These tests pin exactly that:

* ``is_decoy`` is true for each decoy, false for a real tool name.
* OFF by default: a fresh ``McpSurface`` emits NO decoys and never trips on a decoy
  call (behaves like any unknown tool) — ``list_tools`` stays byte-identical.
* ON (opt-in): the decoys appear in ``list_tools``; calling one is refused, the
  upstream client is NEVER invoked, and a ``surf.blocked`` event fires with
  ``decision="honeypot"`` + ``reasons=[HONEYPOT_REASON]``.
* Control-plane: the honeypot event carries the sanitized ``session_id`` fingerprint
  and the code-constant signal — never an arg value, never a decoy payload.
* A legit call with honeypots ON is unaffected (still goes through the normal gate).

Offline throughout — a light fake client records calls; no network, no spec.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko.events import set_surf_sink_override
from gecko.honeypot import (
    HONEYPOT_REASON,
    decoy_tool_defs,
    is_decoy,
)
from gecko.mcp_server import McpSurface

SECRET_ARG = "topsecret-note-DO-NOT-PERSIST-42"


class _RecordingClient:
    """A light fake ``AgentApiClient`` that records every ``call`` — so a test can
    prove the upstream API was (not) invoked. Comprehends one real tool: ``get_odds``."""

    surface_id = "fake-surface"
    # A real client always carries the scale gate (like surface_id). True = below scale
    # (full defs), which is the regime these honeypot tests assert decoys append to.
    surface_all = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.operations: list[Any] = []
        self._tool = {
            "name": "get_odds",
            "description": "Read live odds for a fixture.",
            "inputSchema": {
                "type": "object",
                "properties": {"fixtureId": {"type": "integer"}},
                "required": ["fixtureId"],
            },
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [self._tool]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def call(
        self, name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        self.calls.append((name, dict(args)))
        return {"status": 200, "mode": mode, "data": {"ok": True}}


@pytest.fixture(autouse=True)
def _capture_events():
    """Capture surf events into a list and reset the sink around every test."""
    events: list[dict[str, Any]] = []
    set_surf_sink_override(lambda doc: events.append(dict(doc)))
    try:
        yield events
    finally:
        set_surf_sink_override(None)


# --------------------------------------------------------------------------- #
# is_decoy
# --------------------------------------------------------------------------- #
def test_is_decoy_true_for_each_decoy():
    names = {d["name"] for d in decoy_tool_defs()}
    assert names  # there IS a decoy set
    for name in names:
        assert is_decoy(name) is True


def test_is_decoy_false_for_a_real_tool_name():
    assert is_decoy("get_odds") is False
    assert is_decoy("search_capabilities") is False
    assert is_decoy("comprehend_api") is False


def test_decoy_defs_have_the_tool_shape():
    for d in decoy_tool_defs():
        assert set(d) >= {"name", "description", "inputSchema"}
        assert d["inputSchema"]["type"] == "object"


# --------------------------------------------------------------------------- #
# OFF by default
# --------------------------------------------------------------------------- #
def test_off_by_default_list_tools_has_no_decoys(monkeypatch):
    monkeypatch.delenv("GECKO_HONEYPOTS", raising=False)
    surface = McpSurface(_RecordingClient(), enforce="off")  # type: ignore[arg-type]
    names = {t["name"] for t in surface.list_tools()}
    assert not (names & {d["name"] for d in decoy_tool_defs()})


def test_off_by_default_list_tools_is_byte_identical(monkeypatch):
    # honeypots OFF must not perturb list_tools at all.
    monkeypatch.delenv("GECKO_HONEYPOTS", raising=False)
    client = _RecordingClient()
    baseline = McpSurface(client, enforce="off", honeypots=False)  # type: ignore[arg-type]
    plain = McpSurface(client, enforce="off")  # type: ignore[arg-type]
    assert plain.list_tools() == baseline.list_tools()


def test_off_by_default_decoy_call_does_not_trip(_capture_events):
    # With honeypots off, a decoy name is just an unknown tool — no honeypot signal.
    client = _RecordingClient()
    surface = McpSurface(client, enforce="off")  # type: ignore[arg-type]
    surface.call_tool("admin_export", {"confirm": True})
    assert not [e for e in _capture_events if e.get("decision") == "honeypot"]


# --------------------------------------------------------------------------- #
# ON (opt-in)
# --------------------------------------------------------------------------- #
def test_on_list_tools_includes_the_decoys():
    surface = McpSurface(_RecordingClient(), enforce="off", honeypots=True)  # type: ignore[arg-type]
    names = {t["name"] for t in surface.list_tools()}
    assert {d["name"] for d in decoy_tool_defs()} <= names


def test_on_decoy_call_is_refused_and_upstream_never_invoked(_capture_events):
    client = _RecordingClient()
    surface = McpSurface(client, enforce="off", honeypots=True)  # type: ignore[arg-type]

    out = surface.call_tool("dump_credentials", {"confirm": True})

    assert out["blocked"] is True
    assert client.calls == []  # PROVEN: no upstream call (there is no upstream)
    blocked = [e for e in _capture_events if e["event"] == "surf.blocked"]
    assert len(blocked) == 1
    assert blocked[0]["decision"] == "honeypot"
    assert blocked[0]["reasons"] == [HONEYPOT_REASON]


def test_on_decoy_event_leaks_no_args_and_carries_only_fingerprint(_capture_events):
    client = _RecordingClient()
    surface = McpSurface(client, enforce="off", honeypots=True)  # type: ignore[arg-type]

    surface.call_tool(
        "export_all_secrets",
        {"confirm": True, "note": SECRET_ARG},
        session_id="corr-abc-123",
    )

    blocked = [e for e in _capture_events if e["event"] == "surf.blocked"]
    assert len(blocked) == 1
    event = blocked[0]
    raw = json.dumps(event)
    # The arg VALUE never rides out on the event.
    assert SECRET_ARG not in raw
    # The fingerprint IS present: the sanitized correlation token + code-constant signal.
    assert event["session_id"] == "corr-abc-123"
    assert event["reasons"] == [HONEYPOT_REASON]
    # The decoy NAME is a code constant (spec-derived), safe to record.
    assert event["tool_name"] == "export_all_secrets"


def test_legit_call_with_honeypots_on_is_unaffected(_capture_events):
    client = _RecordingClient()
    surface = McpSurface(client, enforce="block", honeypots=True)  # type: ignore[arg-type]

    out = surface.call_tool("get_odds", {"fixtureId": 4242})

    assert out["status"] == 200  # real (fake) upstream result flowed back
    assert client.calls == [("get_odds", {"fixtureId": 4242})]
    assert not [e for e in _capture_events if e.get("decision") == "honeypot"]


def test_env_opt_in_enables_honeypots(monkeypatch):
    monkeypatch.setenv("GECKO_HONEYPOTS", "1")
    surface = McpSurface(_RecordingClient(), enforce="off")  # type: ignore[arg-type]
    assert surface.honeypots is True
    names = {t["name"] for t in surface.list_tools()}
    assert {d["name"] for d in decoy_tool_defs()} <= names
