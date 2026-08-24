"""Reading a remote surface list at boot — and every way it must refuse.

Two tests carry the weight. The shadowing one, because a remote row overriding a built-in
name would swap a live surface's tools for somebody else's under the same public URL. And
the fail-soft ones, because a host that will not boot when a sign-up service hiccups takes
EVERY existing mount offline to protect one that was not there yet.
"""

from __future__ import annotations

import json

import pytest

from gecko import provider_sync
from gecko.surfaces import _DRAFT_MARKER
from gecko.provider_sync import (
    MAX_SURFACES,
    PROVIDER_SYNC_TOKEN_ENV,
    PROVIDER_SYNC_URL_ENV,
    fetch_provider_surfaces,
)

URL = "https://app.example.test/api/providers/surfaces"
TOKEN = "shared-secret"
SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Acme", "version": "1"},
    "paths": {"/x": {}},
}


def _row(name: str, *, status: str = "active", spec: dict | None = None) -> dict:
    return {"name": name, "status": status, "spec": SPEC if spec is None else spec}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(PROVIDER_SYNC_URL_ENV, URL)
    monkeypatch.setenv(PROVIDER_SYNC_TOKEN_ENV, TOKEN)
    return monkeypatch


def _serve(monkeypatch, payload, *, capture: dict | None = None):
    def fake_get(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr(provider_sync, "safe_get", fake_get)


def test_active_surfaces_are_mounted(configured) -> None:
    _serve(configured, {"surfaces": [_row("acme"), _row("beta")]})
    assert [n for n, _ in fetch_provider_surfaces()] == ["acme", "beta"]


def test_the_shared_token_is_sent_as_the_agreed_header(configured) -> None:
    seen: dict = {}
    _serve(configured, {"surfaces": []}, capture=seen)
    fetch_provider_surfaces()
    assert seen["headers"] == {"X-Provider-Host-Token": TOKEN}
    assert seen["url"] == URL


def test_a_row_may_never_shadow_a_built_in_name(configured) -> None:
    """THE test. Overriding `jito` would replace a live surface's tools with these."""
    _serve(configured, {"surfaces": [_row("jito"), _row("acme")]})
    got = fetch_provider_surfaces(reserved_names=["jito", "orquestra"])
    assert [n for n, _ in got] == ["acme"]


@pytest.mark.parametrize(
    "spec",
    [
        # Recovered from human docs rather than an OpenAPI file — born quarantined.
        {**SPEC, "info": {**SPEC["info"], "x-generated-by": _DRAFT_MARKER}},
        # A flag the anti-poisoning pass left on the surface.
        {**SPEC, "paths": {"/x": {"x-poison": True}}},
    ],
    ids=["from-docs-draft", "poison-flagged"],
)
def test_a_quarantined_spec_is_refused_however_the_status_reads(
    configured, spec
) -> None:
    """The control plane promises clean surfaces, and says status "active" here.

    We check anyway. This mount is PUBLIC and UNAUTHENTICATED, so a bug in somebody
    else's status column must not be able to put a poisoned surface on the internet
    under our hostname.
    """
    _serve(configured, {"surfaces": [_row("acme", spec=spec)]})
    assert fetch_provider_surfaces() == []


def test_non_active_rows_are_skipped(configured) -> None:
    _serve(configured, {"surfaces": [_row("acme", status="pending"), _row("beta")]})
    assert [n for n, _ in fetch_provider_surfaces()] == ["beta"]


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps({"surfaces": "not a list"}),
        json.dumps({"wrong_key": [{"name": "acme"}]}),
        json.dumps([{"name": "acme"}]),  # active-less row
        json.dumps({"surfaces": [{"status": "active", "spec": SPEC}]}),  # no name
        json.dumps({"surfaces": [{"name": "acme", "status": "active"}]}),  # no spec
        json.dumps({"surfaces": ["a string", 42, None]}),
    ],
)
def test_a_malformed_response_yields_nothing_and_never_raises(
    configured, payload
) -> None:
    _serve(configured, payload)
    assert fetch_provider_surfaces() == []


def test_a_transport_failure_serves_built_ins_rather_than_killing_boot(
    configured,
) -> None:
    """A host that will not boot because a sign-up service is down is the worse outage."""

    def explode(url, **kwargs):
        raise TimeoutError("upstream is down")

    configured.setattr(provider_sync, "safe_get", explode)
    assert fetch_provider_surfaces() == []


def test_unconfigured_does_not_even_attempt_a_fetch(monkeypatch) -> None:
    monkeypatch.delenv(PROVIDER_SYNC_URL_ENV, raising=False)
    monkeypatch.delenv(PROVIDER_SYNC_TOKEN_ENV, raising=False)

    def explode(url, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fetched despite being unconfigured")

    monkeypatch.setattr(provider_sync, "safe_get", explode)
    assert fetch_provider_surfaces() == []


def test_the_ssm_sentinel_counts_as_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv(PROVIDER_SYNC_URL_ENV, "__unset__")
    monkeypatch.setenv(PROVIDER_SYNC_TOKEN_ENV, "__unset__")

    def explode(url, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fetched with a sentinel URL")

    monkeypatch.setattr(provider_sync, "safe_get", explode)
    assert fetch_provider_surfaces() == []


def test_a_url_without_a_token_is_a_misconfiguration_not_an_anonymous_fetch(
    monkeypatch,
) -> None:
    """The endpoint is gated; fetching anonymously would 401 on every single boot."""
    monkeypatch.setenv(PROVIDER_SYNC_URL_ENV, URL)
    monkeypatch.delenv(PROVIDER_SYNC_TOKEN_ENV, raising=False)

    def explode(url, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fetched without a token")

    monkeypatch.setattr(provider_sync, "safe_get", explode)
    assert fetch_provider_surfaces() == []


def test_duplicate_names_keep_the_first(configured) -> None:
    first = {**SPEC, "info": {"title": "First", "version": "1"}}
    second = {**SPEC, "info": {"title": "Second", "version": "1"}}
    _serve(
        configured, {"surfaces": [_row("acme", spec=first), _row("acme", spec=second)]}
    )
    got = fetch_provider_surfaces()
    assert len(got) == 1
    assert got[0][1]["info"]["title"] == "First"


def test_the_row_cap_truncates_rather_than_exhausting_boot(configured) -> None:
    _serve(
        configured, {"surfaces": [_row(f"acme{n}") for n in range(MAX_SURFACES + 25)]}
    )
    assert len(fetch_provider_surfaces()) == MAX_SURFACES


def test_names_are_normalized_through_the_engines_own_slugifier(configured) -> None:
    """Our slug is the mount identity — a second slugifier is a 404 nobody can explain."""
    _serve(configured, {"surfaces": [_row("Acme  Widgets, Inc.")]})
    assert [n for n, _ in fetch_provider_surfaces()] == ["acme-widgets-inc"]
