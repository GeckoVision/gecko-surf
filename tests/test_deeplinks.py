"""Deeplink/add-string formatting — decode each back to its host-app contract."""

import base64
import json
from urllib.parse import parse_qs, unquote, urlsplit

from gecko.deeplinks import (
    all_add_strings,
    claude_add_command,
    cursor_deeplink,
    vscode_deeplink,
)

NAME = "pegana"
URL = "https://mcp.gecko.dev/mcp"


def test_claude_add_command_exact():
    assert claude_add_command(NAME, URL) == (
        "claude mcp add --transport http pegana https://mcp.gecko.dev/mcp"
    )


def test_cursor_deeplink_decodes_to_server_config():
    link = cursor_deeplink(NAME, URL)
    assert link.startswith("cursor://anysphere.cursor-deeplink/mcp/install?")
    qs = parse_qs(urlsplit(link).query)
    assert qs["name"] == [NAME]
    config = json.loads(base64.b64decode(unquote(qs["config"][0])))
    assert config == {"url": URL}


def test_vscode_deeplink_decodes_to_http_server():
    link = vscode_deeplink(NAME, URL)
    assert link.startswith("vscode:mcp/install?")
    payload = json.loads(unquote(link.split("?", 1)[1]))
    assert payload == {"name": NAME, "type": "http", "url": URL}


def test_all_add_strings_has_three_hosts():
    out = all_add_strings(NAME, URL)
    assert set(out) == {"claude", "cursor", "vscode"}
    assert out["claude"].startswith("claude mcp add")
