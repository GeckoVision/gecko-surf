"""JSON:API bracketed param names (`filter[user]`) must produce valid tool keys AND
still call the API with the real name. Regression: Privy's spec has 14 such params,
which made Anthropic reject the tool defs (400) — the agent couldn't use them at all.
"""

from __future__ import annotations

import re

from gecko import AgentApiClient
from gecko.access import public_session

_ANTHROPIC_KEY = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

_BRACKET_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "JsonApi", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/assets": {
            "get": {
                "operationId": "listAssets",
                "parameters": [
                    {
                        "name": "filter[user]",
                        "in": "query",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "page[size]",
                        "in": "query",
                        "schema": {"type": "integer"},
                    },
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


def test_bracket_keys_sanitized_valid_and_translated_back():
    c = AgentApiClient(
        _BRACKET_SPEC, base_url="https://api.example.com", session=public_session()
    )
    tool = c.list_tools()[0]
    keys = list(tool["inputSchema"]["properties"])
    # every agent-facing key is Anthropic-valid (no brackets)
    assert all(_ANTHROPIC_KEY.match(k) for k in keys), keys
    assert "filter[user]" not in keys and "page[size]" not in keys
    # the agent supplies the sanitized key; the built request carries the REAL param name
    safe_filter = next(k for k in keys if k.startswith("filter"))
    req = c.prepare("listAssets", {safe_filter: "u1"})
    assert (
        "filter%5Buser%5D=u1" in req.url
    )  # urlencoded `filter[user]=u1`, the real name


def test_plain_param_names_unchanged():
    """A well-formed spec must be byte-identical — no aliases, keys unchanged."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "Plain", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/x/{id}": {
                "get": {
                    "operationId": "getX",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    c = AgentApiClient(
        spec, base_url="https://api.example.com", session=public_session()
    )
    tool = c.list_tools()[0]
    assert list(tool["inputSchema"]["properties"]) == ["id"]
    assert (
        tool["_invoke"].get("arg_aliases") == {}
    )  # no aliases when nothing was sanitized
