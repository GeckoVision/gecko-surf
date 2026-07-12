"""Finding 1 regression: the sealed key must actually reach the served surface.

`gecko add` seals the key in the OS keychain and wires `gecko serve <cache> --stdio
--auth-keychain <surface>` into Claude Code. This proves the OTHER end of that wire:
building the session exactly the way `gecko serve --auth-keychain` does resolves a
FAKE credential and injects it under the header/scheme the SPEC itself declares —
never a hardcoded Bearer. No network, no real keychain.
"""

from __future__ import annotations

from gecko.access import keychain_session
from gecko.credentials import ChainResolver, CredentialRef

_APIKEY_HEADER_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Widget API", "version": "1"},
    "components": {
        "securitySchemes": {
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
        }
    },
    "paths": {},
}

_HTTP_BEARER_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Bearer API", "version": "1"},
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
    },
    "paths": {},
}

_QUERY_APIKEY_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Query API", "version": "1"},
    "components": {
        "securitySchemes": {
            "apiKeyAuth": {"type": "apiKey", "in": "query", "name": "key"}
        }
    },
    "paths": {},
}


class _FakeBackend:
    """Light in-memory keychain fake — deterministic, no OS keychain, no network."""

    name = "fake"

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


def _resolver(secret: str) -> ChainResolver:
    return ChainResolver([_FakeBackend({"widget-api": secret})])


def test_apikey_header_spec_injects_raw_value_under_declared_header():
    session, warning = keychain_session(
        _APIKEY_HEADER_SPEC, "widget-api", resolver=_resolver("sk-live-abc123")
    )
    assert warning is None
    assert session.auth_headers() == {"X-Api-Key": "sk-live-abc123"}


def test_http_bearer_spec_injects_bearer_prefixed_value():
    session, warning = keychain_session(
        _HTTP_BEARER_SPEC, "widget-api", resolver=_resolver("sk-live-abc123")
    )
    assert warning is None
    assert session.auth_headers() == {"Authorization": "Bearer sk-live-abc123"}


def test_unsupported_query_apikey_falls_back_to_public_session_with_warning():
    # query/cookie placement is the same unsafe-location line tools.py already
    # draws for tool visibility — ResolvedSession can't correctly express it, so
    # this must degrade (never crash, never silently mis-inject into a header).
    session, warning = keychain_session(
        _QUERY_APIKEY_SPEC, "widget-api", resolver=_resolver("sk-live-abc123")
    )
    assert warning is not None
    assert "widget-api" in warning
    assert session.auth_headers() == {}


def test_resolves_fresh_from_the_credential_chain_not_hardcoded():
    # Two different surfaces, two different specs, two different resolved secrets —
    # proves the value comes from the injected resolver each time, never a constant.
    session_a, _ = keychain_session(
        _APIKEY_HEADER_SPEC, "widget-api", resolver=_resolver("secret-a")
    )
    session_b, _ = keychain_session(
        _HTTP_BEARER_SPEC, "widget-api", resolver=_resolver("secret-b")
    )
    assert session_a.auth_headers()["X-Api-Key"] == "secret-a"
    assert session_b.auth_headers()["Authorization"] == "Bearer secret-b"
