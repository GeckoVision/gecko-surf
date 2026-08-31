"""Is this tool result a FAILURE? — the one place both MCP transports ask.

MCP's low-level server marks a tool result ``isError: false`` whenever the handler
returns content, and ``isError`` is the ONLY signal an agent has that a call did not
work. So a live 404 that came back as ``{"status": 404, "data": ""}`` was handed to the
agent as a successful call with empty data: a silent wrong answer, in the one product
whose claim is first-call-correct. The upstream failure has to travel as a failure.

The decision lives here (the package is the product) and both transports —
``http_server`` (Streamable HTTP) and ``mcp_server.serve_stdio`` — are thin, so the two
wires can never diverge on what counts as an error. It is API-agnostic: it reads only
the result shapes the ENGINE produces, never anything provider-specific.

The three shapes an engine result can fail in:

* HTTP surfaces — ``client.call`` returns ``{"status": <int>, ...}``; >= 400 is upstream
  saying no (in recorded/probe mode the status is synthesized, and a synthesized 4xx is
  still the API's own "you called this wrong" — the agent should self-heal from it).
* Program surfaces (ore, meteora, ...) — no HTTP status at all; they answer an
  unresolvable request with a structured ``{"error": ...}``.
* Refusals — the risk gate / honeypot / fail-closed paths return ``{"blocked": true}``.
  The call never executed, so it is not a result the agent may act on.

The full body always still reaches the agent (error or not): ``isError`` flags it,
never swallows it, because the body is what the agent self-heals from.
"""

from __future__ import annotations

import json
from typing import Any

#: Below this, upstream is answering; at or above it, upstream is refusing.
HTTP_ERROR_FLOOR = 400


def is_upstream_failure(result: Any) -> bool:
    """True iff ``result`` (an engine tool result) represents a call that did not succeed.

    Conservative by construction — it only fires on shapes the engine itself produces,
    so a provider payload that happens to carry an ``error`` field of its own inside
    ``data`` is untouched. ``error: null`` (how several APIs spell "no error") is not a
    failure.
    """
    if not isinstance(result, dict):
        return False  # search hits / graphs / plain lists: nothing to fail
    status = result.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        if status >= HTTP_ERROR_FLOOR:
            return True
    if result.get("error") not in (None, "", {}, []):
        return True
    return result.get("blocked") is True


def ensure_known_tool(surface: Any, name: str) -> None:
    """Raise the JSON-RPC unknown-tool error when ``name`` is not a tool of ``surface``.

    MCP makes this a PROTOCOL error (-32602 Invalid params), not a tool result: a
    client library maps it to its own exception type, and an auditor probing error
    handling looks for the structured ``error.code`` + ``error.message`` envelope.
    Before this, an unknown name fell through to the Skill Guard and came back as a
    blocked tool RESULT — structured, but on the wrong layer. The message names the
    valid tools so the agent's next call can be right.

    Lives here (with the error decision both transports share) so the HTTP and stdio
    wires cannot diverge. Conservative on purpose: a surface whose ``list_tools``
    itself fails must not turn every call into "unknown tool".
    """
    try:
        names = sorted(str(t["name"]) for t in surface.list_tools())
    except Exception:  # noqa: BLE001 - enumeration failure is not the caller's error
        return
    if name in names:
        return
    from mcp.shared.exceptions import McpError
    from mcp.types import INVALID_PARAMS, ErrorData

    raise McpError(
        ErrorData(
            code=INVALID_PARAMS,
            message=f"Unknown tool: {name!r}. This surface serves: {', '.join(names)}",
        )
    )


def tool_result_payload(result: Any) -> tuple[str, bool]:
    """``(json text, is_error)`` for one MCP ``CallToolResult``.

    The text is the SAME serialization both transports already sent (never cached, never
    persisted); only the error flag is new.
    """
    return json.dumps(result, default=str), is_upstream_failure(result)


__all__ = [
    "HTTP_ERROR_FLOOR",
    "ensure_known_tool",
    "is_upstream_failure",
    "tool_result_payload",
]
