"""The onboard ping — `gecko add` adopters become visible (client side).

Founder-approved design: **default-on, aggregate-only, opt-out** (GECKO_TELEMETRY=off),
fully control-plane (invariant #1). These tests pin the four client guarantees:

  a. a successful add POSTs EXACTLY the five allowlisted keys — host/version/os/
     install_id/mode — and no secret-shaped value, with the transparency line printed;
  b. GECKO_TELEMETRY=off ⇒ the transport is never called and NOTHING is printed;
  c. a raising transport (URLError/timeout) can never break `gecko add`;
  d. the install id persists (same value across calls), is uuid4-hex shaped, random —
     never user-derived — and sits at ~/.gecko/install_id with 0600 perms.

Offline by construction: the POST seam is injected (mirrors login's transport seam);
``AddDeps.ping_post=None`` (the library default) sends nothing, so every other add
test stays network-silent.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error

import pytest

from gecko import onboard
from gecko.netguard import UnsafeUrlError
from gecko.onboard import (
    ONBOARD_PING_NOTE,
    ONBOARD_PING_URL,
    AddDeps,
    add,
    read_or_create_install_id,
)
from gecko.sanitize import looks_like_secret_value

_NO_AUTH_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Open", "version": "1"},
    "paths": {},
}

_PING_KEYS = {"surface_host", "version", "client_os", "install_id", "mode"}

# The exact transparency line the founder approved — non-negotiable: a default-on
# ping the user cannot see would be spyware. Pinned literally so the constant can
# never drift silently.
_EXPECTED_NOTE = (
    "  · anonymous onboard ping (host, version, os — GECKO_TELEMETRY=off to disable)"
)


def _fake_resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise UnsafeUrlError(f"unresolvable test host: {host}")
        return mapping[host]

    return resolve


PUBLIC = _fake_resolver({"api.stripe.com": ["93.184.216.34"]})


def _deps(tmp_path, ping_post=None, resolver=PUBLIC) -> AddDeps:
    return AddDeps(
        fetch=lambda u: json.dumps(_NO_AUTH_SPEC),
        comprehend=lambda spec: 3,
        prompt=lambda q: "unused",
        store=lambda n, s: True,
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=resolver,
        ping_post=ping_post,
    )


# --------------------------------------------------------------------------- #
# (a) the successful-add ping: exact keys, no secrets, transparency line
# --------------------------------------------------------------------------- #
def test_add_pings_with_exactly_the_allowed_keys(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)  # default-on is the design
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))

    rc = add("https://api.stripe.com/openapi.json", deps=deps)

    assert rc == 0
    assert len(pings) == 1
    url, payload = pings[0]
    assert url == ONBOARD_PING_URL
    assert set(payload) == _PING_KEYS  # exactly the allowlist — nothing else leaves
    assert payload["surface_host"] == "api.stripe.com"  # host only, no path/creds
    assert "/openapi.json" not in json.dumps(payload)
    assert payload["mode"] == "recorded"
    assert re.fullmatch(r"[0-9a-f]{32}", payload["install_id"])  # uuid4 hex, opaque
    assert payload["version"]  # the CLI package version string
    assert (
        payload["client_os"] in {"linux", "darwin", "windows"}
        or payload["client_os"] == payload["client_os"].lower()
    )
    assert not any(looks_like_secret_value(v) for v in payload.values())

    out = capsys.readouterr().out
    assert ONBOARD_PING_NOTE == _EXPECTED_NOTE
    assert _EXPECTED_NOTE in out.splitlines()


def test_add_mode_live_pings_mode_live(tmp_path, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))
    add("https://api.stripe.com/openapi.json", mode="live", deps=deps)
    assert pings[0][1]["mode"] == "live"


def test_local_path_add_pings_host_local_never_a_filesystem_path(tmp_path, monkeypatch):
    # A local-path ref must NOT leak the path (it can carry a username); the host is
    # the literal "local" so the adopter still counts, aggregate-only.
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_NO_AUTH_SPEC))
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(
        tmp_path,
        ping_post=lambda url, payload: pings.append((url, payload)),
        resolver=None,
    )
    rc = add(str(spec_path), deps=deps)
    assert rc == 0
    payload = pings[0][1]
    assert payload["surface_host"] == "local"
    assert str(tmp_path) not in json.dumps(payload)


def test_library_default_sends_nothing(tmp_path, capsys, monkeypatch):
    # AddDeps.ping_post=None (an embedded onboard.add) stays network-silent; only the
    # CLI wires the real transport. No note is printed for a ping that never went out.
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    rc = add("https://api.stripe.com/openapi.json", deps=_deps(tmp_path))
    assert rc == 0
    assert "onboard ping" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (b) opt-out: GECKO_TELEMETRY=off ⇒ no transport call, nothing printed
# --------------------------------------------------------------------------- #
def test_telemetry_off_never_calls_transport_and_prints_nothing(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))

    rc = add("https://api.stripe.com/openapi.json", deps=deps)

    assert rc == 0
    assert pings == []
    assert "onboard ping" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# (c) a raising transport can never break `gecko add`
# --------------------------------------------------------------------------- #
def test_transport_urlerror_never_breaks_add(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)

    def boom(url: str, payload: dict[str, str]) -> None:
        raise urllib.error.URLError("timed out")

    rc = add(
        "https://api.stripe.com/openapi.json", deps=_deps(tmp_path, ping_post=boom)
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "ask your agent" in out.lower()  # add completed all the way
    assert "onboard ping" not in out  # never claim a send that failed


def test_transport_arbitrary_exception_never_breaks_add(tmp_path, monkeypatch):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)

    def boom(url: str, payload: dict[str, str]) -> None:
        raise RuntimeError("anything at all")

    rc = add(
        "https://api.stripe.com/openapi.json", deps=_deps(tmp_path, ping_post=boom)
    )
    assert rc == 0


# --------------------------------------------------------------------------- #
# (d) install_id: persisted, stable, uuid4-hex, 0600, never user-derived
# --------------------------------------------------------------------------- #
def test_install_id_persists_and_is_uuid_hex(tmp_path):
    first = read_or_create_install_id(tmp_path)
    second = read_or_create_install_id(tmp_path)
    assert first == second  # persisted once, stable across calls
    assert re.fullmatch(r"[0-9a-f]{32}", first)  # uuid4().hex — random, opaque
    path = tmp_path / ".gecko" / "install_id"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_two_adds_of_different_surfaces_share_the_same_install_id(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))
    add("https://api.stripe.com/openapi.json", deps=deps)
    add("https://api.stripe.com/openapi.json", name="stripe-two", deps=deps)
    assert len(pings) == 2
    assert pings[0][1]["install_id"] == pings[1][1]["install_id"]


def test_a_fresh_home_mints_a_new_install_id(tmp_path):
    # Counting people = one id per machine, never one per person: a NEW home mints a
    # NEW random id (no user-derived/machine-derived value could ever tie them).
    first = read_or_create_install_id(tmp_path / "home-a")
    second = read_or_create_install_id(tmp_path / "home-b")
    assert first != second


def test_install_id_write_is_atomic_never_a_partial_file(tmp_path, monkeypatch):
    # The persist step must be write-tmp-then-rename: if the final rename never
    # happens, NO install_id file may exist (a torn/empty file would make every
    # later run re-mint, which is exactly the field bug: N pings, N distinct ids).
    def no_replace(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", no_replace)
    got = read_or_create_install_id(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", got)  # still returns a usable per-run id
    assert not (tmp_path / ".gecko" / "install_id").exists()


def test_unwritable_home_degrades_to_per_run_ids_and_never_crashes(
    tmp_path, monkeypatch
):
    # An unwritable HOME (read-only container, sandboxed agent shell) must degrade
    # toward counting — an ephemeral id and a ping per run — never a crash and never
    # a lost adopter.
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)  # ~/.gecko can never be created
    try:
        first = read_or_create_install_id(home)
        second = read_or_create_install_id(home)
        assert re.fullmatch(r"[0-9a-f]{32}", first)
        assert first != second  # nothing persisted — per-run ids, honestly
        pings: list[tuple[str, dict[str, str]]] = []
        for _ in range(2):
            onboard.send_onboard_ping(
                ref="https://api.stripe.com/openapi.json",
                base_url=None,
                mode="recorded",
                home=home,
                post=lambda url, payload: pings.append((url, payload)),
                surface="api-stripe-com",
            )
        assert len(pings) == 2  # the once-marker can't persist either: count per run
    finally:
        home.chmod(0o700)


# --------------------------------------------------------------------------- #
# (e) idempotence: re-adding the SAME surface from the same install pings ONCE
# --------------------------------------------------------------------------- #
def test_readding_the_same_surface_pings_once(tmp_path, capsys, monkeypatch):
    # The field bug: every re-run of `gecko add` (quickstart runs it three times,
    # agents retry after a flag fix) re-pinged, so the adoption metric counted runs,
    # not adopters. A (install, surface) pair must count once.
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))
    add("https://api.stripe.com/openapi.json", deps=deps)
    add("https://api.stripe.com/openapi.json", deps=deps)
    assert len(pings) == 1
    # The transparency note prints only for the ping that actually left.
    out = capsys.readouterr().out
    assert out.count(ONBOARD_PING_NOTE) == 1


def test_failed_ping_leaves_no_marker_so_the_adopter_still_counts_later(
    tmp_path, monkeypatch
):
    # A dead network on the first add must NOT permanently mark the surface as
    # pinged — the next successful add still counts the adopter.
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)

    def boom(url: str, payload: dict[str, str]) -> None:
        raise urllib.error.URLError("network down")

    add("https://api.stripe.com/openapi.json", deps=_deps(tmp_path, ping_post=boom))
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))
    add("https://api.stripe.com/openapi.json", deps=deps)
    assert len(pings) == 1


def test_telemetry_off_leaves_no_marker(tmp_path, monkeypatch):
    # Opting out must not burn the once-per-surface marker: opting back in later
    # still counts the adopter.
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    deps_off = _deps(tmp_path, ping_post=lambda url, payload: None)
    add("https://api.stripe.com/openapi.json", deps=deps_off)
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))
    add("https://api.stripe.com/openapi.json", deps=deps)
    assert len(pings) == 1


# --------------------------------------------------------------------------- #
# (f) the ping URL is resolved at CALL time, not bound at import time
# --------------------------------------------------------------------------- #
def test_ping_url_is_resolved_at_call_time(tmp_path, monkeypatch):
    # A dev/test harness that redirects ``onboard.ONBOARD_PING_URL`` must actually
    # take effect. The old default-arg binding froze the PRODUCTION URL at import
    # time, so harness runs (fresh tmp homes, adds in quick succession) silently
    # posted to the real ingest — polluting the adoption metric with distinct-id
    # ping pairs.
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    monkeypatch.setattr(onboard, "ONBOARD_PING_URL", "https://example.test/redirected")
    pings: list[tuple[str, dict[str, str]]] = []
    deps = _deps(tmp_path, ping_post=lambda url, payload: pings.append((url, payload)))
    add("https://api.stripe.com/openapi.json", deps=deps)
    assert pings[0][0] == "https://example.test/redirected"
