"""The authenticated spec-bearing door: /comprehend/servable.

The test that matters most is the fail-closed one. `/comprehend` is public and returns a
summary; this sibling returns the PARSED SPEC, so left open it would be a general
fetch-and-parse proxy for any URL a stranger names. With no token configured it must be
indistinguishable from a route that does not exist.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko.comprehend_service import (  # noqa: E402
    ComprehendResult,
    ServableResult,
    servable_payload,
)
from gecko.http_server import (  # noqa: E402
    SERVABLE_PATH,
    SERVABLE_TOKEN_ENV,
    SERVABLE_TOKEN_HEADER,
    SERVABLE_TOKEN_SENTINEL,
    build_multi_surface_app,
)

PEGANA = "tests/fixtures/pegana_openapi.json"
TOKEN = "s3cret-token-value"


def _canned_summary() -> ComprehendResult:
    return ComprehendResult(
        name="Acme Widgets",
        description="stub",
        op_count=2,
        usable_tool_count=2,
        tools=[{"name": "list_widgets", "summary": "list them"}],
        artifacts={"llms.txt": "x", "gecko.json": "{}"},
        quarantined=False,
        warnings=[],
        next_steps={"self_host": "uvx ..."},
    )


def _canned() -> ServableResult:
    return ServableResult(
        slug="acme-widgets",
        spec={"openapi": "3.1.0", "paths": {"/widgets": {}}},
        summary=_canned_summary(),
    )


def _app() -> Any:
    return build_multi_surface_app([("pegana", PEGANA)], allowed_hosts=["testserver"])


def _stub(monkeypatch) -> None:
    monkeypatch.setattr("gecko.comprehend_service.ensure_submittable", lambda _u: None)
    monkeypatch.setattr(
        "gecko.comprehend_service.comprehend_servable",
        lambda url, from_docs=False: _canned(),
    )


def test_absent_when_no_token_is_configured(monkeypatch) -> None:
    """THE test. An operator who never set the secret has not opened the door — and a
    prober cannot tell 'disabled' from 'never routed'."""
    monkeypatch.delenv(SERVABLE_TOKEN_ENV, raising=False)
    _stub(monkeypatch)
    with TestClient(_app()) as c:
        r = c.post(SERVABLE_PATH, json={"url": "https://acme.test/openapi.json"})
    assert r.status_code == 404


def test_a_wrong_token_is_also_a_404_not_a_403(monkeypatch) -> None:
    """A 403 would confirm the endpoint exists and invite guessing; 404 says nothing."""
    monkeypatch.setenv(SERVABLE_TOKEN_ENV, TOKEN)
    _stub(monkeypatch)
    with TestClient(_app()) as c:
        r = c.post(
            SERVABLE_PATH,
            json={"url": "https://acme.test/openapi.json"},
            headers={SERVABLE_TOKEN_HEADER: "wrong"},
        )
    assert r.status_code == 404


def test_the_right_token_returns_the_spec_and_the_canonical_slug(monkeypatch) -> None:
    monkeypatch.setenv(SERVABLE_TOKEN_ENV, TOKEN)
    _stub(monkeypatch)
    with TestClient(_app()) as c:
        r = c.post(
            SERVABLE_PATH,
            json={"url": "https://acme.test/openapi.json"},
            headers={SERVABLE_TOKEN_HEADER: TOKEN},
        )
    assert r.status_code == 200
    body = r.json()
    # The two things the public summary door deliberately lacks.
    assert body["spec"] == {"openapi": "3.1.0", "paths": {"/widgets": {}}}
    assert body["slug"] == "acme-widgets"
    assert body["quarantined"] is False
    assert body["tools"] == [{"name": "list_widgets", "summary": "list them"}]


def test_the_public_summary_door_still_never_returns_a_spec(monkeypatch) -> None:
    """The whole reason this is a separate route: /comprehend's contract is unchanged."""
    monkeypatch.setattr("gecko.comprehend_service.ensure_submittable", lambda _u: None)
    monkeypatch.setattr(
        "gecko.comprehend_service.comprehend_submission",
        lambda url, from_docs=False: _canned_summary(),
    )
    with TestClient(_app()) as c:
        r = c.post("/comprehend", json={"url": "https://acme.test/openapi.json"})
    assert r.status_code == 200
    assert "spec" not in r.json()


def test_the_slug_is_normalized_once_so_two_slugifiers_cannot_disagree() -> None:
    """A name with punctuation is where a second slugifier drifts from ours."""
    from gecko.surfaces import safe_surface_id

    messy = _canned_summary()
    result = ServableResult(
        slug=safe_surface_id("Acme  Widgets, Inc."),
        spec={},
        summary=messy,
    )
    assert servable_payload(result)["slug"] == "acme-widgets-inc"


def test_a_missing_url_is_refused_before_anything_is_fetched(monkeypatch) -> None:
    monkeypatch.setenv(SERVABLE_TOKEN_ENV, TOKEN)
    _stub(monkeypatch)
    with TestClient(_app()) as c:
        r = c.post(SERVABLE_PATH, json={}, headers={SERVABLE_TOKEN_HEADER: TOKEN})
    assert r.status_code == 400
    assert "url" in r.json()["error"]


def test_the_ssm_boot_sentinel_is_not_a_usable_token(monkeypatch) -> None:
    """An ECS `Secrets:` ValueFrom must resolve or the task dies at boot, so every wired
    param is pushed with the `__unset__` placeholder when it has no real value.

    That placeholder is a literal in a PUBLIC repository. If it were honoured as a real
    secret, a sentinel-provisioned deploy would accept it from anyone who read the file —
    the value that exists to keep the door SHUT would be the key that opens it.
    """
    monkeypatch.setenv(SERVABLE_TOKEN_ENV, SERVABLE_TOKEN_SENTINEL)
    _stub(monkeypatch)
    with TestClient(_app()) as c:
        r = c.post(
            SERVABLE_PATH,
            json={"url": "https://acme.test/openapi.json"},
            headers={SERVABLE_TOKEN_HEADER: SERVABLE_TOKEN_SENTINEL},
        )
    assert r.status_code == 404


def test_whitespace_around_a_real_token_does_not_defeat_it(monkeypatch) -> None:
    """SSM values pick up trailing newlines; the env side is stripped, so a correct
    token must still match rather than silently closing the door."""
    monkeypatch.setenv(SERVABLE_TOKEN_ENV, f"  {TOKEN}\n")
    _stub(monkeypatch)
    with TestClient(_app()) as c:
        r = c.post(
            SERVABLE_PATH,
            json={"url": "https://acme.test/openapi.json"},
            headers={SERVABLE_TOKEN_HEADER: TOKEN},
        )
    assert r.status_code == 200
