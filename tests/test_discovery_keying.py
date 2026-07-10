"""Bug #1 (discovery keying) regression lock — the 'works on API #1, breaks on API #2'
failure. The catalog's key and the tool's name must be the SAME string, or
`client.search()` (which filters hits against usable tool names) drops every result.
The historical risk was a spec with NO operationIds (ingest synthesizes them) or ids
that change under sanitization. This pins that search() is non-empty + keys agree.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko.access import public_session
from gecko.client import AgentApiClient

# A spec that omits operationId entirely AND has path chars that must be sanitized —
# exactly the shape that used to return 0 tools from search().
_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "No-OperationId API", "version": "1.0.0"},
    "servers": [{"url": "https://example.com"}],
    "paths": {
        "/api/v1/charges/{id}": {
            "get": {
                "summary": "Fetch a charge by its id",
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
        },
        "/api/v1/refunds": {
            "post": {
                "summary": "Create a refund for a charge",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _client(tmp_path: Path) -> AgentApiClient:
    p = tmp_path / "no_opid.json"
    p.write_text(json.dumps(_SPEC), "utf-8")
    return AgentApiClient(str(p), session=public_session())


def test_search_is_not_empty_on_operationid_less_spec(tmp_path: Path) -> None:
    c = _client(tmp_path)
    assert len(c.list_tools()) == 2  # both ops surfaced
    hits = c.search("get a charge by id", limit=3)
    assert hits, "search() returned NOTHING — the keying bug is back (bug #1)"
    assert hits[0]["name"] in {t["name"] for t in c.list_tools()}


def test_catalog_key_equals_tool_name(tmp_path: Path) -> None:
    # The load-bearing invariant: every catalog entry's key IS a real tool name.
    c = _client(tmp_path)
    tool_names = {t["name"] for t in c.list_tools()}
    for q in ["fetch a charge", "create a refund"]:
        for h in c.search(q, limit=3):
            assert h["name"] in tool_names
