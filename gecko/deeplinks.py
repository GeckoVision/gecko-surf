"""One-click add strings for the hosted MCP surface.

Pure string formatting — given a server name and the served MCP URL, emit the
copy-paste / deeplink each host app understands so a human can connect an external
agent in one step. No network, no state.

Formats (host-app conventions, pinned by the M1 plan):
- Claude Code: the ``claude mcp add --transport http <name> <url>`` CLI line.
- Cursor: ``cursor://anysphere.cursor-deeplink/mcp/install?name=…&config=<base64>``
  where ``config`` is base64(JSON) of the mcp.json server entry — ``{"url": url}``
  for a remote streamable-HTTP server.
- VS Code: ``vscode:mcp/install?<url-encoded JSON>`` of ``{name, type:"http", url}``.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import quote

CURSOR_SCHEME = "cursor://anysphere.cursor-deeplink/mcp/install"
VSCODE_SCHEME = "vscode:mcp/install"


def claude_add_command(name: str, url: str) -> str:
    """The Claude Code CLI line to add this server over Streamable HTTP."""
    return f"claude mcp add --transport http {name} {url}"


def cursor_deeplink(name: str, url: str) -> str:
    """A Cursor one-click ``cursor://`` deeplink (base64 server config)."""
    config = base64.b64encode(json.dumps({"url": url}).encode("utf-8")).decode("ascii")
    return f"{CURSOR_SCHEME}?name={quote(name)}&config={quote(config)}"


def vscode_deeplink(name: str, url: str) -> str:
    """A VS Code one-click ``vscode:mcp/install`` deeplink (url-encoded JSON)."""
    payload = json.dumps({"name": name, "type": "http", "url": url})
    return f"{VSCODE_SCHEME}?{quote(payload)}"


def all_add_strings(name: str, url: str) -> dict[str, str]:
    """Every supported add string, keyed by host app — for the serve CLI banner."""
    return {
        "claude": claude_add_command(name, url),
        "cursor": cursor_deeplink(name, url),
        "vscode": vscode_deeplink(name, url),
    }
