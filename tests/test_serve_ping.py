"""The serve first-run ping — `gecko serve` installs become visible (client side).

`gecko serve` (and the Claude-plugin /make-agent-ready path, which runs serve) is the
newest install channel, and it previously emitted NO first-run signal. Same design as
the `gecko add` onboard ping: default-on, aggregate-only, opt-out (GECKO_TELEMETRY=off),
the same five-key envelope with ``mode="serve"``, and the same transparency line — but
fired ONCE per install+surface (a local marker next to the install-id file), never per
boot: a Claude-wired stdio surface is re-spawned every session.

Offline by construction: the POST seam is injected; ``serve.main`` sends nothing unless
its caller (the CLI dispatcher) wires the real transport.
"""

from __future__ import annotations

import json

import pytest

from gecko import onboard
from gecko.onboard import ONBOARD_PING_NOTE, send_serve_ping

_PING_KEYS = {"surface_host", "version", "client_os", "install_id", "mode"}

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Open", "version": "1"},
    "paths": {},
}


def _collect(pings):
    return lambda url, payload: pings.append((url, payload))


# --------------------------------------------------------------------------- #
# The envelope: same five keys, mode="serve", host attribution, note on stderr
# --------------------------------------------------------------------------- #
def test_serve_ping_sends_mode_serve_with_the_exact_envelope(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    send_serve_ping(
        ref="https://api.stripe.com/openapi.json",
        base_url=None,
        home=tmp_path,
        post=_collect(pings),
    )
    assert len(pings) == 1
    _, payload = pings[0]
    assert set(payload) == _PING_KEYS
    assert payload["mode"] == "serve"
    assert payload["surface_host"] == "api.stripe.com"
    captured = capsys.readouterr()
    # serve's stdout can BE the MCP stdio JSON-RPC channel — the transparency line
    # must ride stderr, and must still be the exact founder-approved line.
    assert ONBOARD_PING_NOTE in captured.err
    assert ONBOARD_PING_NOTE not in captured.out


def test_serve_ping_local_path_never_leaks_the_filesystem_path(tmp_path, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_SPEC))
    pings: list[tuple[str, dict[str, str]]] = []
    send_serve_ping(
        ref=str(spec_path), base_url=None, home=tmp_path, post=_collect(pings)
    )
    payload = pings[0][1]
    assert payload["surface_host"] == "local"
    assert str(tmp_path) not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Once per install+surface — never per boot
# --------------------------------------------------------------------------- #
def test_serve_ping_fires_once_per_install_and_surface(tmp_path, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    for _ in range(3):  # a stdio surface is spawned once per agent session
        send_serve_ping(
            ref="https://api.stripe.com/openapi.json",
            base_url=None,
            home=tmp_path,
            post=_collect(pings),
        )
    assert len(pings) == 1
    # A DIFFERENT surface on the same install still counts (same install_id).
    send_serve_ping(
        ref="https://api.twilio.com/openapi.json",
        base_url=None,
        home=tmp_path,
        post=_collect(pings),
    )
    assert len(pings) == 2
    assert pings[0][1]["install_id"] == pings[1][1]["install_id"]


def test_add_then_wired_serve_of_the_same_surface_pings_once_total(
    tmp_path, monkeypatch
):
    # `gecko add` wires Claude to spawn `gecko serve ~/.gecko/surfaces/<slug>.json`.
    # The add already counted this install+surface; the wired spawn must not
    # double-count it — the marker is keyed by the surface slug, which the cache
    # file's stem preserves.
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    deps = onboard.AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 1,
        prompt=lambda q: "",
        store=lambda n, s: True,
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=lambda host: ["93.184.216.34"],
        ping_post=_collect(pings),
    )
    assert onboard.add("https://api.stripe.com/openapi.json", deps=deps) == 0
    assert len(pings) == 1
    cache = tmp_path / ".gecko" / "surfaces" / "api-stripe-com.json"
    assert cache.exists()
    send_serve_ping(
        ref=str(cache),
        base_url="https://api.stripe.com",
        home=tmp_path,
        post=_collect(pings),
    )
    assert len(pings) == 1  # already counted by the add


# --------------------------------------------------------------------------- #
# Opt-out, failure honesty
# --------------------------------------------------------------------------- #
def test_serve_ping_telemetry_off_sends_and_prints_nothing(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    pings: list[tuple[str, dict[str, str]]] = []
    send_serve_ping(
        ref="https://api.stripe.com/openapi.json",
        base_url=None,
        home=tmp_path,
        post=_collect(pings),
    )
    assert pings == []
    captured = capsys.readouterr()
    assert "onboard ping" not in captured.err + captured.out


def test_serve_ping_failure_never_raises_and_leaves_no_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)

    def boom(url: str, payload: dict[str, str]) -> None:
        raise OSError("network down")

    send_serve_ping(
        ref="https://api.stripe.com/openapi.json",
        base_url=None,
        home=tmp_path,
        post=boom,
    )
    pings: list[tuple[str, dict[str, str]]] = []
    send_serve_ping(
        ref="https://api.stripe.com/openapi.json",
        base_url=None,
        home=tmp_path,
        post=_collect(pings),
    )
    assert len(pings) == 1  # the failed attempt did not burn the marker


# --------------------------------------------------------------------------- #
# The CLI wiring: serve.main fires it; library callers stay network-silent
# --------------------------------------------------------------------------- #
def test_serve_main_fires_the_first_run_ping_when_wired(tmp_path, monkeypatch):
    serve = pytest.importorskip("gecko.serve")
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_SPEC))
    monkeypatch.setattr("gecko.serve.serve_stdio", lambda *a, **k: None)
    pings: list[tuple[str, dict[str, str]]] = []
    rc = serve.main([str(spec_path), "--stdio"], ping_post=_collect(pings))
    assert rc == 0
    assert len(pings) == 1
    assert pings[0][1]["mode"] == "serve"


def test_serve_main_default_sends_nothing(tmp_path, monkeypatch):
    # Library/test use of serve.main (no ping_post) stays network-silent — only the
    # CLI dispatcher wires the real transport, mirroring AddDeps.ping_post.
    serve = pytest.importorskip("gecko.serve")
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_SPEC))
    monkeypatch.setattr("gecko.serve.serve_stdio", lambda *a, **k: None)
    called: list[object] = []
    monkeypatch.setattr(onboard, "_default_ping_post", lambda u, p: called.append(u))
    rc = serve.main([str(spec_path), "--stdio"])
    assert rc == 0
    assert called == []
