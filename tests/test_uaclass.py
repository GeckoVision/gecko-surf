"""Robot/human classification of a connecting MCP client (``gecko.uaclass``).

Pure, offline: the whole classification is a deterministic ordered rule list, so these
assert the rules directly — no server, no network.
"""

from __future__ import annotations

import pytest

from gecko.events import CLIENT_KINDS
from gecko.uaclass import classify_client


@pytest.mark.parametrize(
    "ua",
    [
        "python-requests/2.31.0",
        "curl/8.4.0",
        "Go-http-client/2.0",
        "agent-tools/1.0",
        "verifymcp/0.1",
        "Censys/1.0",
        "some-crawler-bot/1",
        "Mozilla/5.0 (compatible; SemrushBot/7)",
    ],
)
def test_robot_user_agents(ua: str) -> None:
    assert classify_client(ua, None) == "robot"


@pytest.mark.parametrize(
    "ua,client",
    [
        ("claude-code/1.2.3", None),
        ("cursor-vscode/0.42", None),
        (None, "Claude Code/1.0"),
        (None, "cursor"),
        ("modelcontextprotocol-client/1", None),
        ("mcp-remote/0.3", None),
    ],
)
def test_real_mcp_clients(ua: str | None, client: str | None) -> None:
    assert classify_client(ua, client) == "client"


@pytest.mark.parametrize(
    "ua",
    [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "",
        None,
    ],
)
def test_unknown_when_nothing_matches(ua: str | None) -> None:
    assert classify_client(ua, None) == "unknown"


def test_robot_wins_a_tie() -> None:
    # A crawler that fakes a real client NAME in clientInfo but connects with a
    # python-requests UA is still a robot — the robot rules are checked first.
    assert classify_client("python-requests/2.31", "claude-code/1.0") == "robot"


def test_result_is_always_a_closed_set_member() -> None:
    for ua, client in [
        ("curl/8", None),
        ("claude-code/1", None),
        ("Mozilla/5.0", None),
        (None, None),
    ]:
        assert classify_client(ua, client) in CLIENT_KINDS
