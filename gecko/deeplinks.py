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


def claude_add_command(
    name: str, url: str, headers: dict[str, str] | None = None
) -> str:
    """The Claude Code CLI line to add this server over Streamable HTTP.

    ``headers`` (when given) are appended as ``--header "K: V"`` flags — the seam that
    attaches the anon-first attribution header (``anon.anon_connect_headers``) so a
    connect our CLI configured is joinable to the install per person. Empty/None keeps
    the line byte-identical to before."""
    line = f"claude mcp add --transport http {name} {url}"
    for key, value in (headers or {}).items():
        line += f' --header "{key}: {value}"'
    return line


def claude_stdio_add_command(name: str, spawn: str) -> str:
    """The Claude Code CLI line to add this server over **stdio** — the client spawns
    ``spawn`` as a subprocess and talks over stdin/stdout. No port, no tunnel: the
    zero-friction local path. ``spawn`` is the exact command that launches the server
    in ``--stdio`` mode (e.g. ``uvx --from "gecko-surf[serve]" colosseum-mcp --stdio``
    or ``gecko <spec> --stdio``)."""
    return f"claude mcp add {name} -- {spawn}"


def cursor_deeplink(name: str, url: str, headers: dict[str, str] | None = None) -> str:
    """A Cursor one-click ``cursor://`` deeplink (base64 server config).

    ``headers`` ride in the server config JSON (``{"url", "headers"}``) — how a Cursor
    mcp.json entry carries request headers. Empty/None keeps the ``{"url"}``-only config
    byte-identical to before."""
    config_obj: dict[str, object] = {"url": url}
    if headers:
        config_obj["headers"] = headers
    config = base64.b64encode(json.dumps(config_obj).encode("utf-8")).decode("ascii")
    return f"{CURSOR_SCHEME}?name={quote(name)}&config={quote(config)}"


def vscode_deeplink(name: str, url: str, headers: dict[str, str] | None = None) -> str:
    """A VS Code one-click ``vscode:mcp/install`` deeplink (url-encoded JSON).

    ``headers`` ride in the server entry (``{name, type, url, headers}``) — how a VS Code
    mcp.json HTTP server carries request headers. Empty/None keeps it byte-identical."""
    payload_obj: dict[str, object] = {"name": name, "type": "http", "url": url}
    if headers:
        payload_obj["headers"] = headers
    payload = json.dumps(payload_obj)
    return f"{VSCODE_SCHEME}?{quote(payload)}"


def all_add_strings(
    name: str, url: str, headers: dict[str, str] | None = None
) -> dict[str, str]:
    """Every supported add string, keyed by host app — for the serve CLI banner.

    ``headers`` (from ``anon.anon_connect_headers``) are threaded into each host-app form
    so any hosted connect our CLI hands the developer is joinable to the install per
    person. Empty/None (telemetry off) keeps every string byte-identical."""
    return {
        "claude": claude_add_command(name, url, headers),
        "cursor": cursor_deeplink(name, url, headers),
        "vscode": vscode_deeplink(name, url, headers),
    }
