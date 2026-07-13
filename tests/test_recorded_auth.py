"""Recorded mode must not inject auth (or run its host guard).

Regression for the first-run rough edge: `gecko test <url-spec> --mode recorded` on an
auth-gated API with no base_url failed the well-formed checks with "refusing to inject
auth toward unexpected host" — because prepare injected auth even in recorded mode, and
the synthesized host didn't match the spec-pinned anchor. Recorded never hits the wire,
so auth is a live-only concern.
"""

from __future__ import annotations

from gecko import AgentApiClient
from gecko.access import stub_session

_AUTH_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Gated", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "components": {"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}},
    "security": [{"b": []}],
    "paths": {
        "/thing/{id}": {
            "get": {
                "operationId": "getThing",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        }
    },
}


def _client() -> AgentApiClient:
    # base_url pins the anchor; stub_session supplies a bearer token (no real secret).
    return AgentApiClient(
        _AUTH_SPEC, base_url="https://api.example.com", session=stub_session()
    )


def test_prepare_injects_auth_by_default_but_skips_when_disabled():
    c = _client()
    with_auth = c.prepare("getThing", {"id": "1"}, inject_auth=True)
    assert with_auth.headers.get("Authorization", "").startswith("Bearer ")
    without = c.prepare("getThing", {"id": "1"}, inject_auth=False)
    assert "Authorization" not in without.headers


def test_recorded_call_does_not_inject_auth():
    """A recorded call goes through the inject_auth=False path — synthesized, no auth."""
    c = _client()
    res = c.call("getThing", {"id": "1"}, mode="recorded")
    assert res["status"] == 200 and res["mode"] == "recorded"
