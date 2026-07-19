"""Multi-scheme auth injection — the two-token case (TxLINE: httpAuth + apiKeyAuth).

`gecko serve --auth-keychain <surface>` must inject EVERY header-shaped security
scheme the spec declares, not just the first. A single served call then carries
both `Authorization: Bearer <jwt>` AND `X-Api-Token: <tok>` — the shape an
AND-secured endpoint requires. Proven with a fake resolver: no OS keychain, no
network, $0. Each scheme's secret is keyed by the scheme NAME (`api:schemeName`)
so `gecko auth set <surface> --account <schemeName>` seals the right one.
"""

from __future__ import annotations

from gecko.access import (
    MultiSession,
    ResolvedSession,
    auth_setup_hint,
    keychain_session,
)
from gecko.caller import build_request
from gecko.credentials import ChainResolver, CredentialRef

# Two header-shaped schemes declared together — TxLINE's real shape.
_TWO_TOKEN_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "TxLINE-like", "version": "1"},
    "components": {
        "securitySchemes": {
            "httpAuth": {"type": "http", "scheme": "bearer"},
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Token"},
        }
    },
    "paths": {},
}

# A header scheme paired with an UNSUPPORTED one (apiKey-in-query) — the query
# scheme must be skipped, never mis-injected into a header.
_MIXED_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Mixed", "version": "1"},
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
            "queryKey": {"type": "apiKey", "in": "query", "name": "key"},
        }
    },
    "paths": {},
}

_SINGLE_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Single", "version": "1"},
    "components": {
        "securitySchemes": {
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
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


def _resolver(store: dict[str, str]) -> ChainResolver:
    return ChainResolver([_FakeBackend(store)])


def test_multisession_merges_every_sessions_headers() -> None:
    inner = [
        ResolvedSession(
            CredentialRef(api="s", account="a"),
            "Authorization",
            scheme="bearer",
            resolver=_resolver({"s:a": "jwt"}),
        ),
        ResolvedSession(
            CredentialRef(api="s", account="b"),
            "X-Api-Token",
            resolver=_resolver({"s:b": "tok"}),
        ),
    ]
    session = MultiSession(inner)
    assert session.auth_headers() == {
        "Authorization": "Bearer jwt",
        "X-Api-Token": "tok",
    }


def test_two_token_spec_builds_a_multisession_keyed_by_scheme_name() -> None:
    store = {"txline:httpAuth": "jwt-x", "txline:apiKeyAuth": "tok-y"}
    session, warning = keychain_session(
        _TWO_TOKEN_SPEC, "txline", resolver=_resolver(store)
    )
    assert warning is None
    assert isinstance(session, MultiSession)
    assert session.auth_headers() == {
        "Authorization": "Bearer jwt-x",
        "X-Api-Token": "tok-y",
    }


def test_both_tokens_flow_through_the_unchanged_caller() -> None:
    # The seam did not move: a MultiSession is just an AuthSession, and both
    # headers land on the PreparedRequest the existing caller builds.
    store = {"txline:httpAuth": "jwt-x", "txline:apiKeyAuth": "tok-y"}
    session, _ = keychain_session(_TWO_TOKEN_SPEC, "txline", resolver=_resolver(store))
    tool = {"_invoke": {"method": "GET", "path": "/api/x", "param_locations": {}}}
    req = build_request(tool, {}, "https://txline.example", auth=session.auth_headers())
    assert req.headers["Authorization"] == "Bearer jwt-x"
    assert req.headers["X-Api-Token"] == "tok-y"


def test_unsupported_scheme_among_supported_is_skipped_not_misinjected() -> None:
    # Only ONE scheme survives filtering (the query key is dropped), so this takes
    # the single-scheme path — a plain ResolvedSession keyed with no account.
    store = {"mixed": "jwt"}
    session, warning = keychain_session(_MIXED_SPEC, "mixed", resolver=_resolver(store))
    assert warning is None
    assert isinstance(session, ResolvedSession)
    assert session.auth_headers() == {"Authorization": "Bearer jwt"}


def test_single_scheme_stays_a_plain_resolvedsession_account_none() -> None:
    # Backward compatibility: a one-scheme spec must behave exactly as before —
    # a ResolvedSession keyed with NO account (slot == the bare surface name).
    session, warning = keychain_session(
        _SINGLE_SPEC, "single", resolver=_resolver({"single": "sk-1"})
    )
    assert warning is None
    assert isinstance(session, ResolvedSession)
    assert session.auth_headers() == {"X-Api-Key": "sk-1"}


def test_auth_setup_hint_lists_a_set_command_per_scheme() -> None:
    hint = auth_setup_hint(_TWO_TOKEN_SPEC, "txline")
    assert hint is not None
    assert "gecko auth set txline --account httpAuth --scheme bearer" in hint
    assert "gecko auth set txline --account apiKeyAuth" in hint
    # the bearer flag rides only the bearer scheme, not the raw apiKey one.
    assert "--account apiKeyAuth --scheme bearer" not in hint


def test_auth_setup_hint_is_none_for_single_scheme() -> None:
    # One scheme needs no per-scheme guidance — the plain flow already covers it.
    assert auth_setup_hint(_SINGLE_SPEC, "single") is None
