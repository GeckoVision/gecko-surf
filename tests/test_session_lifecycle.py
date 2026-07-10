"""Falsifier-first (Pattern B, offline, $0) for the session-lifecycle refresh core.

These tests exist and must FAIL before any lifecycle impl. They drive the whole
Spec-A core through a FakeTransport (no network, no keys):

  * proactive  — a session within its expiry leeway refreshes INSIDE auth_headers();
  * reactive   — a live call that 401s self-heals in EXACTLY ONE retry, then 200s;
  * seam identity — a plain AuthSession (no invalidate/expires_at) is byte-identical;
  * bounded    — a second consecutive 401 raises a typed AuthError (no infinite loop);
  * recorded   — mode="recorded" makes ZERO network calls through the lifecycle.

Everything is offline: the API-call transport and the OAuth token transport are both
injected, so a passing run here falsifies the implementation without a subscription.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.access import (
    AuthError,
    OAuth2Lifecycle,
    Session,
    is_refreshable,
    oauth2_from_dpo2u,
)
from gecko.caller import PreparedRequest
from gecko.client import AgentApiClient
from gecko.credentials import ChainResolver, CredentialRef

# --- offline fixtures --------------------------------------------------------

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "ping the service",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


class _FakeBackend:
    """Deterministic in-memory credential backend (no network)."""

    name = "fake"

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


def _resolver(refresh_token: str) -> ChainResolver:
    return ChainResolver([_FakeBackend({"dpo2u": refresh_token})])


class FakeRefreshable:
    """A minimal RefreshableSession: emits a bearer token that advances on invalidate().

    ``tokens=["STALE", "FRESH"]`` models a rejected-then-valid credential. It performs
    NO network itself — the reactive/bounded/recorded paths are exercised purely through
    the client's injected API transport.
    """

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = list(tokens)
        self._i = 0
        self.invalidate_calls = 0

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens[self._i]}"}

    def invalidate(self) -> None:
        self.invalidate_calls += 1
        if self._i < len(self._tokens) - 1:
            self._i += 1

    def expires_at(self) -> float | None:
        return None


def _client(session: Any, transport: Any) -> AgentApiClient:
    return AgentApiClient(
        SPEC,
        base_url="https://api.example.com",
        session=session,
        live_transport=transport,
    )


def _ping(client: AgentApiClient) -> str:
    return client.list_tools()[0]["name"]


# --- reactive: bounded-once self-heal ---------------------------------------


def test_reactive_self_heal_one_retry_then_succeeds() -> None:
    seen: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        bearer = req.headers.get("Authorization", "")
        seen.append(bearer)
        if "STALE" in bearer:
            return 401, {"error": "token expired"}
        return 200, {"ok": True}

    session = FakeRefreshable(["STALE", "FRESH"])
    client = _client(session, transport)
    result = client.call(_ping(client), {}, mode="live")

    assert result["status"] == 200
    assert result["mode"] == "live"
    # exactly one retry: first request stale, retry carried the fresh token.
    assert seen == ["Bearer STALE", "Bearer FRESH"]
    assert session.invalidate_calls == 1


def test_second_consecutive_401_raises_bounded_auth_error() -> None:
    calls: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        calls.append(req.headers.get("Authorization", ""))
        return 401, {"error": "revoked"}

    # invalidate advances but the credential is still rejected -> bounded stop.
    session = FakeRefreshable(["STALE", "STILL_STALE"])
    client = _client(session, transport)
    with pytest.raises(AuthError):
        client.call(_ping(client), {}, mode="live")

    # bounded: original + exactly one retry, then stop. NO unbounded re-auth loop.
    assert len(calls) == 2
    assert session.invalidate_calls == 1


# --- seam identity: a plain AuthSession is byte-identical --------------------


def test_non_refreshable_session_is_byte_identical() -> None:
    calls: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        calls.append(req.headers.get("Authorization", ""))
        return 200, {"ok": True}

    session = Session(jwt="JWT", api_token="APITOK")
    assert not is_refreshable(session)  # no invalidate/expires_at -> not refreshable

    client = _client(session, transport)
    result = client.call(_ping(client), {}, mode="live")
    # No retry, no invalidate path — the self-heal hook is inert for a plain session.
    assert result["status"] == 200
    assert len(calls) == 1
    # header dict unchanged from today
    assert session.auth_headers() == {
        "Authorization": "Bearer JWT",
        "X-Api-Token": "APITOK",
    }


# --- recorded mode: zero network through the lifecycle ----------------------


def test_recorded_mode_makes_zero_network_calls() -> None:
    def transport(req: PreparedRequest) -> tuple[int, Any]:
        raise AssertionError("recorded mode must not hit the transport")

    session = FakeRefreshable(["TOK"])
    client = _client(session, transport)
    result = client.call(_ping(client), {}, mode="recorded")

    assert result["mode"] == "recorded"
    assert session.invalidate_calls == 0  # lifecycle never engaged


# --- proactive refresh inside auth_headers() --------------------------------


def _token_transport(new_token: str, sink: list[Any]):
    def transport(url: str, form: dict[str, str]) -> tuple[int, Any]:
        sink.append((url, form))
        return 200, {"access_token": new_token, "expires_in": 3600}

    return transport


def test_proactive_refresh_when_within_leeway() -> None:
    sink: list[Any] = []
    now = 1000.0
    session = OAuth2Lifecycle(
        token_endpoint="https://mcp.dpo2u.com/token",
        refresh_ref=CredentialRef(api="dpo2u"),
        resolver=_resolver("REFRESH_TKN"),
        leeway=60.0,
        transport=_token_transport("NEW_ACCESS", sink),
        clock=lambda: now,
        access_token="OLD_ACCESS",
        exp=now + 30,  # within the 60s leeway -> must refresh before returning headers
    )
    headers = session.auth_headers()

    assert headers == {"Authorization": "Bearer NEW_ACCESS"}
    assert len(sink) == 1
    _, form = sink[0]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "REFRESH_TKN"


def test_valid_token_does_not_refresh() -> None:
    sink: list[Any] = []
    now = 1000.0
    session = OAuth2Lifecycle(
        token_endpoint="https://mcp.dpo2u.com/token",
        refresh_ref=CredentialRef(api="dpo2u"),
        resolver=_resolver("REFRESH_TKN"),
        leeway=60.0,
        transport=_token_transport("NEW_ACCESS", sink),
        clock=lambda: now,
        access_token="STILL_GOOD",
        exp=now + 3600,  # comfortably ahead of leeway -> no refresh
    )
    assert session.auth_headers() == {"Authorization": "Bearer STILL_GOOD"}
    assert sink == []


def test_oauth2_is_refreshable_and_repr_is_secret_free() -> None:
    session = OAuth2Lifecycle(
        token_endpoint="https://mcp.dpo2u.com/token",
        refresh_ref=CredentialRef(api="dpo2u"),
        resolver=_resolver("SECRET_REFRESH"),
        access_token="SECRET_ACCESS",
        exp=1.0,
    )
    assert is_refreshable(session)
    text = repr(session)
    assert "SECRET_ACCESS" not in text
    assert "SECRET_REFRESH" not in text
    assert "dpo2u" in text  # the non-secret ref/endpoint is fine to show


def test_refresh_rejected_raises_redacted_auth_error() -> None:
    def transport(url: str, form: dict[str, str]) -> tuple[int, Any]:
        return 401, {"error": "invalid_grant"}

    session = OAuth2Lifecycle(
        token_endpoint="https://mcp.dpo2u.com/token",
        refresh_ref=CredentialRef(api="dpo2u"),
        resolver=_resolver("REFRESH_TKN"),
        transport=transport,
        access_token=None,  # forces a refresh on first auth_headers()
    )
    with pytest.raises(AuthError) as exc:
        session.auth_headers()
    # redact-before-raise: no token in the terminal error
    assert "REFRESH_TKN" not in str(exc.value)


# --- dpo2u thin shape (provider-agnostic core, provider glue is thin) -------


def test_oauth2_from_dpo2u_reads_file_and_refreshes(tmp_path) -> None:
    import json

    sink: list[Any] = []
    oauth_file = tmp_path / "oauth.json"
    oauth_file.write_text(
        json.dumps(
            {
                "access_token": "FILE_ACCESS",
                "refresh_token": "FILE_REFRESH",
                "expires_at": 1000.0 + 5,  # near-expired
            }
        )
    )
    now = 1000.0
    session = oauth2_from_dpo2u(
        path=oauth_file, transport=_token_transport("REFRESHED_ACCESS", sink)
    )
    session.clock = lambda: now  # deterministic clock for the leeway check
    headers = session.auth_headers()

    assert headers == {"Authorization": "Bearer REFRESHED_ACCESS"}
    assert session.token_endpoint == "https://mcp.dpo2u.com/token"
    # the refresh token from the file drove the grant (resolved, never stored on repr)
    assert sink[0][1]["refresh_token"] == "FILE_REFRESH"
    assert "FILE_REFRESH" not in repr(session)
    assert "FILE_ACCESS" not in repr(session)
