"""The plan_payment card — MCP Apps wiring, proven over the real streamable-HTTP wire.

The card's whole safety story is in three properties, each pinned here: it is
self-contained (no external fetch for the CSP to block or a leak to hide in), it renders
only what the tool already returned (structuredContent IS the JSON every other client
gets), and a client that does not speak MCP Apps sees a byte-identical tool.
"""

from __future__ import annotations

import json
import re
from typing import Any

from starlette.testclient import TestClient

from gecko.cards import (
    CARD_MIME_TYPE,
    PLAN_PAYMENT_RESOURCE_URI,
    UI_TOOL_RESOURCES,
    card_html,
    card_resources,
)

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _sse_json(response: Any) -> dict[str, Any]:
    raw = response.text
    if "data:" in raw[:300]:
        raw = [
            line[5:].strip() for line in raw.splitlines() if line.startswith("data:")
        ][-1]
    return json.loads(raw)


# --- the template itself ---------------------------------------------------------


def test_the_card_is_self_contained() -> None:
    """No external script/style/img/font — the sandbox CSP would block them, and a card
    that phones home is a card that leaks. Every byte ships in the resource."""
    html = card_html("plan_payment")
    assert html is not None
    for pattern in (
        r'src\s*=\s*["\']https?://',
        r'href\s*=\s*["\']https?://',
        r"@import",
        r"url\(",
        r"fetch\(",
        r"XMLHttpRequest",
        r"WebSocket",
    ):
        assert not re.search(pattern, html), f"external reference: {pattern}"


def test_the_card_speaks_the_apps_protocol() -> None:
    """The view-side handshake, pinned by string: initialize on load, then the three
    notifications a host sends. A card missing one renders blank in the chat."""
    html = card_html("plan_payment")
    assert html is not None
    for needle in (
        '"initialize"',
        "ui/notifications/tool-result",
        "ui/notifications/tool-cancelled",
        "ui/notifications/host-context-changed",
        "ui/notifications/size-changed",
        "structuredContent",
    ):
        assert needle in html, f"missing protocol piece: {needle}"


def test_the_refusal_renders_as_prominently_as_success() -> None:
    """The refusal is the product. A card with no blocked state — or one that styles it
    as a generic error — would be selling against it."""
    html = card_html("plan_payment")
    assert html is not None
    assert "REFUSED" in html
    assert "blocked" in html
    assert "peg" in html.lower()


def test_every_card_tool_has_a_resource_and_vice_versa() -> None:
    uris = card_resources()
    assert set(UI_TOOL_RESOURCES.values()) == set(uris)
    for uri, (mime, html) in uris.items():
        assert uri.startswith("ui://gecko/")
        assert mime == CARD_MIME_TYPE
        assert html.lstrip().startswith("<!DOCTYPE html>")


# --- the wire, end to end --------------------------------------------------------


def _mcp(
    client: TestClient, payload: dict[str, Any], session: str | None = None
) -> Any:
    headers = dict(HEADERS)
    if session:
        headers["mcp-session-id"] = session
    return client.post("/orq/mcp", json=payload, headers=headers)


def _session(client: TestClient) -> str:
    init = _mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "card-probe", "version": "1"},
            },
        },
    )
    assert init.status_code == 200
    session = init.headers["mcp-session-id"]
    _mcp(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    return session


def _app() -> Any:
    from gecko.http_server import build_multi_surface_app
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    surface = OrquestraCatalogSurface()
    return build_multi_surface_app([("orq", surface)], allowed_hosts=["testserver"])


def test_the_tool_declares_its_card_over_the_wire() -> None:
    with TestClient(_app()) as client:
        session = _session(client)
        listed = _sse_json(
            _mcp(
                client,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                session,
            )
        )
        tools = {t["name"]: t for t in listed["result"]["tools"]}
        assert (
            tools["plan_payment"]["_meta"]["ui"]["resourceUri"]
            == PLAN_PAYMENT_RESOURCE_URI
        )
        # and ONLY card tools carry the meta — everything else is byte-identical
        assert "_meta" not in tools["list_stores"] or "ui" not in tools[
            "list_stores"
        ].get("_meta", {})


def test_the_card_resource_is_served_over_the_wire() -> None:
    with TestClient(_app()) as client:
        session = _session(client)
        read = _sse_json(
            _mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "resources/read",
                    "params": {"uri": PLAN_PAYMENT_RESOURCE_URI},
                },
                session,
            )
        )
        (content,) = read["result"]["contents"]
        assert content["mimeType"] == CARD_MIME_TYPE
        assert content["text"].lstrip().startswith("<!DOCTYPE html>")


def test_a_card_tool_result_carries_structured_content() -> None:
    """The card renders structuredContent — which is the SAME dict the text content
    carries, so nothing new crosses the boundary because the card exists. Driven with a
    refusal the HANDLER produces (a malformed buyer) rather than missing args — the SDK
    validates the input schema before our handler runs, so schema-invalid args never
    reach the code that attaches structuredContent."""
    with TestClient(_app()) as client:
        session = _session(client)
        result = _sse_json(
            _mcp(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "plan_payment",
                        "arguments": {
                            "store": "geckocoffee",
                            "product": "Espresso",
                            "buyer": "not-a-base58-pubkey",
                        },
                    },
                },
                session,
            )
        )["result"]
        structured = result["structuredContent"]
        assert "error" in structured
        assert json.loads(result["content"][0]["text"]) == structured
