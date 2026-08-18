"""A minimal MCP client — for calling SOMEONE ELSE'S MCP server.

Every other MCP module in this package is a *server*: it answers an agent. This one dials
out, because measuring a partner surface means calling it the way an agent would, not the
way its source suggests it behaves.

Streamable HTTP is JSON-RPC over POST. A server may answer with a plain JSON body or with
an SSE frame (``event: message\\ndata: {...}``), and the same server does both depending on
the endpoint, so :func:`_decode` handles either rather than assuming.

READ-ONLY BY DISPOSITION, NOT BY GUARANTEE. Nothing here signs or broadcasts, and the
tools this package calls on a partner surface are listings and derivations. But
``call_tool`` will call whatever it is given: it is the caller's job not to name a tool
that writes. Every request goes through :func:`gecko.netguard.safe_post_json`, so the
SSRF and DNS-rebind defenses are the package's existing ones, not a second copy.

Control-plane invariant #1 holds here as everywhere: responses are returned to the caller
and never persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .netguard import safe_post_json

__all__ = [
    "McpError",
    "McpToolError",
    "McpClient",
    "PostJson",
]

#: The transport seam, injectable so tests can falsify this module offline (Pattern B).
PostJson = Callable[[str, dict[str, Any]], str]


class McpError(Exception):
    """The server did not answer with a usable JSON-RPC result."""


class McpToolError(McpError):
    """The server answered, and the answer is a tool-level error.

    Kept distinct from :class:`McpError` because the two mean opposite things about the
    surface: a transport error says we could not ask, and this says the surface answered
    "no" — which for a measurement is a *result*, not a failure to measure.
    """


def _decode(raw: str) -> dict[str, Any]:
    """One JSON-RPC response, from either a plain body or an SSE frame."""
    text = raw.strip()
    if not text:
        raise McpError("empty response body")
    if not text.startswith("{"):
        # SSE: take the LAST data: line — a stream may carry progress notifications
        # ahead of the result, and the result is what a caller asked for.
        payloads = [
            line[len("data:") :].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        if not payloads:
            raise McpError(f"response is neither JSON nor an SSE frame: {text[:200]!r}")
        text = payloads[-1]
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise McpError(f"response is {type(decoded).__name__}, not a JSON-RPC object")
    return decoded


@dataclass
class McpClient:
    """A client for one MCP endpoint.

    ``post`` is the transport seam. The default reaches the network through the
    package's SSRF guard; a test passes a fake and never leaves the process.
    """

    url: str
    post: PostJson | None = None
    _next_id: int = 0

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        transport = self.post or (lambda url, payload: safe_post_json(url, payload))
        raw = transport(
            self.url,
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params},
        )
        response = _decode(raw)
        if "error" in response:
            error = response["error"] or {}
            raise McpError(
                f"{method} failed: {error.get('message', error)} "
                f"(code {error.get('code', '?')})"
            )
        if "result" not in response:
            raise McpError(f"{method} answered without a result: {response!r}")
        return response["result"]

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the surface offers, as the server describes them."""
        result = self._call("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpError(f"tools/list answered without a tool list: {result!r}")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call one tool and return its text content.

        An ``isError`` result raises :class:`McpToolError` carrying the server's own
        message. Surfacing the message verbatim matters for a measurement: "this tool
        needs an argument you did not pass" and "this program is not in the catalogue"
        are different findings and must not both read as "call failed".
        """
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpError(
                f"tools/call answered {type(result).__name__}, not an object"
            )
        blocks = result.get("content") or []
        text = "\n".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if result.get("isError"):
            raise McpToolError(text or f"{name} reported an error with no message")
        return text
