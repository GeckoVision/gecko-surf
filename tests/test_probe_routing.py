"""Task 1.2 — ``probe`` routes through the surface like recorded (no wire, no auth).

The three assertions that make probe engine-safe:
  * an injected live transport is NEVER invoked (the sandbox sits on the no-wire
    side of the transport edge, invariant #3);
  * the session's auth is never resolved for a probe call (no wire -> no injection);
  * probe outcomes route to the segregated ``synthetic.jsonl``, never the corpus.

Plus the MCP surface threading: ``call_tool`` passes the transport ``session_id``
into the client ONLY in probe mode (it keys the Phase-3 SimWorld), and a legacy
duck-typed client without the kwarg keeps working in every other mode.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko.client import AgentApiClient, ToolNotFound
from gecko.mcp_server import McpSurface

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Pay API", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/balance": {
            "get": {
                "operationId": "getBalance",
                "summary": "Read the account balance.",
                "parameters": [
                    {
                        "name": "account",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    },
                    "422": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error_code": {"type": "string"},
                                        "detail": {"type": "string"},
                                    },
                                    "required": ["error_code"],
                                }
                            }
                        }
                    },
                },
            }
        }
    },
}


class CountingSession:
    """A light fake session that counts auth-header resolutions."""

    def __init__(self) -> None:
        self.resolutions = 0

    def auth_headers(self) -> dict[str, str]:
        self.resolutions += 1
        return {"X-Api-Token": "CANARY_not_a_real_token"}


def _probe_client(**kwargs: Any) -> tuple[AgentApiClient, list[Any], CountingSession]:
    wire_calls: list[Any] = []

    def transport(req: Any) -> tuple[int, Any]:
        wire_calls.append(req)
        return 200, {}

    session = CountingSession()
    client = AgentApiClient(
        SPEC,
        base_url="https://api.example.com",
        session=session,
        live_transport=transport,
        **kwargs,
    )
    return client, wire_calls, session


def test_probe_never_touches_the_transport_or_auth() -> None:
    client, wire_calls, session = _probe_client()
    baseline = session.resolutions  # __init__ resolves once to detect auth capability

    result = client.call("getBalance", {"account": "acct-1"}, mode="probe")

    assert wire_calls == [], "probe must never invoke the live transport"
    assert session.resolutions == baseline, "probe must never resolve auth headers"
    assert result["mode"] == "probe"
    assert result["status"] == 200


def test_probe_returns_the_apis_own_synthetic_error_not_a_raised_callerror() -> None:
    client, wire_calls, _ = _probe_client()

    result = client.call("getBalance", {}, mode="probe")  # missing required 'account'

    assert wire_calls == []
    assert result["status"] == 422
    assert result["mode"] == "probe"
    assert "schema.required" in result["signals"]
    assert "schema.required" in result["remediation"]
    # The body is shaped like THIS API's declared 422 schema (comprehension-native).
    assert result["data"]["error_code"] == "sample"


def test_probe_result_carries_no_filled_url() -> None:
    client, _, _ = _probe_client()
    result = client.call("getBalance", {"account": "acct-1"}, mode="probe")
    # Control plane: the templated path only — never a filled request URL.
    assert "request" not in result
    assert result["path"] == "/balance"


def test_probe_outcome_is_captured_as_synthetic_never_the_main_corpus(
    tmp_path,
) -> None:
    main = tmp_path / "corpus.jsonl"
    client, _, _ = _probe_client(corpus_path=main)

    client.call("getBalance", {}, mode="probe")

    assert not main.exists()
    sibling = main.with_name("synthetic.jsonl")
    assert sibling.exists()
    row = json.loads(sibling.read_text(encoding="utf-8").strip())
    assert row["mode"] == "probe"
    assert row["source"] == "synthetic"
    assert row["error_class"] == "unprocessable_422"


def test_probe_works_on_an_unpinned_surface() -> None:
    # A dict spec with no base_url is unverified (no trust anchor). Live would fail
    # closed; probe never reaches the wire, so the auth/host guard is moot.
    client = AgentApiClient(SPEC)
    result = client.call("getBalance", {"account": "a"}, mode="probe")
    assert result["mode"] == "probe"
    assert result["status"] == 200


def test_probe_unknown_tool_raises_the_typed_error() -> None:
    client, _, _ = _probe_client()
    with pytest.raises(ToolNotFound):
        client.call("noSuchTool", {}, mode="probe")


class RecordingClient:
    """Duck-typed client that records what the MCP surface passes through."""

    surface_id = "fake"

    def __init__(self) -> None:
        self.seen: tuple[Any, ...] | None = None

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": "getBalance", "description": "", "inputSchema": {"type": "object"}}
        ]

    def call(
        self,
        name: str,
        args: dict[str, Any],
        mode: str = "recorded",
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.seen = (name, mode, session_id)
        return {"status": 200, "mode": mode}


class LegacyClient:
    """A duck-typed client WITHOUT the session_id kwarg (e.g. the red-team wrapper)."""

    surface_id = "legacy"

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": "getBalance", "description": "", "inputSchema": {"type": "object"}}
        ]

    def call(
        self, name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        return {"status": 200, "mode": mode}


def test_mcp_surface_threads_session_id_in_probe_mode() -> None:
    client = RecordingClient()
    surface = McpSurface(client, mode="probe", enforce="off")  # type: ignore[arg-type]

    surface.call_tool("getBalance", {}, session_id="sess-1")

    assert client.seen == ("getBalance", "probe", "sess-1")


def test_mcp_surface_does_not_pass_session_id_outside_probe() -> None:
    # A legacy duck-typed client (no session_id kwarg) must keep working unchanged.
    surface = McpSurface(LegacyClient(), mode="recorded", enforce="off")  # type: ignore[arg-type]
    result = surface.call_tool("getBalance", {}, session_id="sess-1")
    assert result["mode"] == "recorded"
