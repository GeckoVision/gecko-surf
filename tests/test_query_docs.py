"""Phase 4 — the ``query_docs`` self-heal MCP tool.

Two things must hold:
  * CONTROL-PLANE gate (invariant #1 / #4): the output is spec-derived ONLY — no auth
    header/token, no private ``_invoke`` routing, no request payload or arg value.
  * a matching intent returns the relevant op's summary + params + inputSchema, so the
    agent can understand WHY a call failed and rewrite it.
"""

from __future__ import annotations

import json
from typing import Any

from gecko.client import AgentApiClient
from gecko.docsearch import search_docs
from gecko.mcp_server import McpSurface

#: A recognizable secret planted on the session — it must never surface in query_docs.
CANARY_TOKEN = "CANARY_super_secret_token_value"

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Pay API", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/withdraw": {
            "post": {
                "operationId": "createWithdraw",
                "summary": "Withdraw funds to a recipient account.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "amount": {
                                        "type": "number",
                                        "description": "How much to withdraw, in minor units.",
                                    },
                                    "to": {
                                        "type": "string",
                                        "description": "Destination account id.",
                                    },
                                },
                                "required": ["amount"],
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/balance": {
            "get": {
                "operationId": "getBalance",
                "summary": "Read the account balance.",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


class SecretSession:
    """A light fake session carrying a canary auth token."""

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {CANARY_TOKEN}"}


def _client() -> AgentApiClient:
    return AgentApiClient(
        SPEC, base_url="https://api.example.com", session=SecretSession()
    )


def test_query_docs_returns_the_matching_ops_summary_and_params() -> None:
    result = search_docs(_client(), "withdraw money to an account")

    assert result["intent"] == "withdraw money to an account"
    names = {m["name"] for m in result["matches"]}
    match = next(
        m for m in result["matches"] if m["name"] in names and "withdraw" in m["path"]
    )
    # spec-derived summary + params surfaced so the agent can rewrite a bad call
    assert "Withdraw funds" in match["summary"]
    param_names = {p["name"] for p in match["params"]}
    assert {"amount", "to"} <= param_names
    amount = next(p for p in match["params"] if p["name"] == "amount")
    assert amount["required"] is True
    assert "minor units" in amount["description"]
    # the callable contract the agent needs to fix the call
    assert result["matches"][0]["inputSchema"]["type"] == "object"


def test_query_docs_is_control_plane_only() -> None:
    result = search_docs(_client(), "withdraw funds")
    blob = json.dumps(result)

    # invariant #1 / #4: nothing secret or wire-shaped leaks into the doc search
    assert CANARY_TOKEN not in blob
    assert "Authorization" not in blob
    assert "_invoke" not in blob
    # no per-match key is an auth or routing field
    for match in result["matches"]:
        assert "_invoke" not in match
        assert "auth" not in {k.lower() for k in match}


def test_mcp_surface_dispatches_query_docs() -> None:
    surface = McpSurface(_client(), enforce="off")
    result = surface.call_tool("query_docs", {"intent": "read the balance"})

    assert "matches" in result
    assert json.dumps(result).find(CANARY_TOKEN) == -1
