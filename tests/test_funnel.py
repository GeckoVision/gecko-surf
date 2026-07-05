"""Unit tests for the funnel aggregation (scripts/funnel.py) on synthetic events.

Offline, no Mongo: ``summarize_funnel`` is a pure function over control-plane-safe
event dicts. Proves the connect -> activate -> return math AND the EXTERNAL-only filter
(our own clients, and any session they own, are excluded).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_FUNNEL = Path(__file__).resolve().parent.parent / "scripts" / "funnel.py"
_spec = importlib.util.spec_from_file_location("funnel", _FUNNEL)
assert _spec and _spec.loader
funnel = importlib.util.module_from_spec(_spec)
sys.modules["funnel"] = funnel  # so @dataclass can resolve the module at class build
_spec.loader.exec_module(funnel)


def _connect(
    surface: str, session: str, client: str = "claude-code/1.0"
) -> dict[str, Any]:
    return {
        "event": "surf.connect",
        "surface_id": surface,
        "session_id": session,
        "client": client,
    }


def _call(surface: str, session: str) -> dict[str, Any]:
    return {"event": "surf.call", "surface_id": surface, "session_id": session}


def _failed(surface: str, client: str = "cursor/0.4") -> dict[str, Any]:
    return {"event": "surf.connect_failed", "surface_id": surface, "client": client}


def _row(rows: list[Any], surface: str) -> Any:
    return next(r for r in rows if r.surface_id == surface)


def test_full_funnel_connect_activate_return():
    events = [
        _connect("pegana", "s1"),
        _connect("pegana", "s2"),
        _connect("pegana", "s3"),  # connects but never calls (installed, didn't use)
        _call("pegana", "s1"),
        _call("pegana", "s1"),  # s1 returned (>=2 calls)
        _call("pegana", "s2"),  # s2 activated only
        _failed("pegana"),
    ]
    row = _row(funnel.summarize_funnel(events), "pegana")
    assert row.connects == 3
    assert row.connect_failed == 1
    assert row.activated == 2  # s1, s2
    assert row.returned == 1  # s1 only
    assert row.activation_rate == 2 / 3
    assert row.retention_rate == 1 / 2


def test_external_filter_excludes_our_own_client_and_its_calls():
    events = [
        _connect("pegana", "ext", client="claude-code/1.0"),
        _connect("pegana", "mine", client="gecko-smoke/9"),  # OURS -> excluded
        _call("pegana", "ext"),
        _call("pegana", "mine"),  # a self session's calls must not count
        _call("pegana", "mine"),
    ]
    row = _row(funnel.summarize_funnel(events), "pegana")
    assert row.connects == 1  # only the external session
    assert row.activated == 1  # only ext
    assert row.returned == 0  # the self session's 2 calls are excluded


def test_env_extra_self_clients_are_honored():
    events = [
        _connect("pegana", "ext", client="claude-code/1.0"),
        _connect("pegana", "smoke", client="my-loadtest/1"),
        _call("pegana", "smoke"),
    ]
    self_clients = funnel._DEFAULT_SELF_CLIENTS | {"my-loadtest"}
    row = _row(funnel.summarize_funnel(events, self_clients=self_clients), "pegana")
    assert row.connects == 1
    assert row.activated == 0  # my-loadtest session excluded


def test_call_without_session_id_is_not_attributed():
    # An aggregate-fallback / legacy call with no session id cannot join a session, so it
    # must not inflate activated/returned (fail closed, not silently count).
    events = [
        _connect("pegana", "s1"),
        {"event": "surf.call", "surface_id": "pegana"},  # no session_id
    ]
    row = _row(funnel.summarize_funnel(events), "pegana")
    assert row.connects == 1
    assert row.activated == 0


def test_multiple_surfaces_are_reported_separately():
    events = [
        _connect("pegana", "a"),
        _call("pegana", "a"),
        _connect("nora", "b"),
    ]
    rows = funnel.summarize_funnel(events)
    assert {r.surface_id for r in rows} == {"pegana", "nora"}
    assert _row(rows, "pegana").activated == 1
    assert _row(rows, "nora").activated == 0


def test_search_counts_as_a_call_for_activation():
    events = [
        _connect("pegana", "s1"),
        {"event": "surf.search", "surface_id": "pegana", "session_id": "s1"},
    ]
    row = _row(funnel.summarize_funnel(events), "pegana")
    assert row.activated == 1


def test_render_is_stable_and_mentions_the_funnel_stages():
    rows = funnel.summarize_funnel(
        [_connect("pegana", "s1"), _call("pegana", "s1"), _call("pegana", "s1")]
    )
    out = funnel.render(rows, days=30, source="test")
    assert "CONNECT" in out
    assert "pegana" in out
    assert "TOTAL" in out


def test_jsonl_fallback_filters_by_window(tmp_path):
    import json
    import time

    now = int(time.time() * 1000)
    old = now - 40 * 86_400_000
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(d)
            for d in [
                {**_connect("pegana", "recent"), "ts": now},
                {**_connect("pegana", "stale"), "ts": old},
                "not json",
            ]
        )
    )
    events = funnel.load_from_jsonl(str(path), days=30)
    sessions = {e.get("session_id") for e in events}
    assert "recent" in sessions
    assert "stale" not in sessions  # outside the 30d window
