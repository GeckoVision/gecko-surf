"""Robot/human classification of a connecting MCP client (``gecko.uaclass``).

Pure, offline: the whole classification is a deterministic ordered rule list, so these
assert the rules directly — no server, no network.
"""

from __future__ import annotations

import pytest

from gecko.events import CLIENT_KINDS
from gecko.uaclass import classify_client, reclassify_client


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


@pytest.mark.parametrize(
    "ua",
    [
        # The MCP directory / indexer fleet observed live as "client"/"unknown" — they
        # are crawlers and must classify as robot (PATTERN-based, not just these literals).
        "glama/1.0.0",
        "agent-tools.cloud/0.1",
        "acton-skill-extractor/0.1.0",
        "verifymcp-probe/1",
        "mcp-indexer/0.1.0",
        # SHAPE, not literal: tomorrow's directory/indexer/probe must be caught too.
        "foo-mcp-indexer/2",
        "bar-probe/0.9",
        "some-skill-extractor/1",
        "acme-directory-crawler/3",
    ],
)
def test_indexer_directory_fleet_is_robot(ua: str) -> None:
    assert classify_client(ua, None) == "robot"


@pytest.mark.parametrize(
    "ua,client",
    [
        # Guard against over-matching: the indexer/probe/extractor patterns must NOT
        # catch a real MCP client (mcp-indexer trips on "indexer", NOT on "mcp-").
        ("claude-code/1.2.3", None),
        ("cursor/0.42", None),
        ("cline/2.0", None),
        ("windsurf/1.0", None),
        ("mcp-remote/0.3", None),
        (None, "Claude Code/1.0"),
    ],
)
def test_indexer_patterns_do_not_over_match_real_clients(
    ua: str | None, client: str | None
) -> None:
    assert classify_client(ua, client) == "client"


@pytest.mark.parametrize(
    "ua_or_client",
    [
        # scraper / verifier / validator markers — real crawlers seen live in the
        # `client` bucket. A genuine interactive MCP client is never named these.
        "mcp-scraper/0.1",
        "liner-mcp-verifier/1",
        "mcp-server-validator/2",
        "mcp-apps-validator/0.3",
    ],
)
def test_scraper_verifier_validator_markers_are_robot(ua_or_client: str) -> None:
    # Match whether the marker arrives via the UA or via clientInfo (both haystacks).
    assert classify_client(ua_or_client, None) == "robot"
    assert classify_client(None, ua_or_client) == "robot"


@pytest.mark.parametrize(
    "ua,client",
    [
        # The new markers must NOT catch a real interactive client.
        ("claude-code/2.1.7", None),
        (None, "Claude Code/2.1.x"),
        ("cursor/0.43", None),
        (None, "Cline/3.0"),
    ],
)
def test_new_markers_do_not_over_match_real_clients(
    ua: str | None, client: str | None
) -> None:
    assert classify_client(ua, client) == "client"


def test_reclassify_row_rederives_from_raw_ignoring_stale_kind() -> None:
    # A row frozen as `client` under old rules, but whose raw clientInfo is a crawler,
    # must re-derive to `robot` on read — proving we never trust the stored kind.
    row = {"client_kind": "client", "client": "mcp-scraper/0.1"}
    assert reclassify_client(row) == "robot"


def test_reclassify_row_rederives_from_user_agent() -> None:
    row = {"client_kind": "client", "user_agent": "python-requests/2.31"}
    assert reclassify_client(row) == "robot"


def test_reclassify_row_falls_back_to_stored_kind_when_no_raw() -> None:
    # A pre-instrumentation row with nothing to re-derive from keeps its stored label.
    assert reclassify_client({"client_kind": "client"}) == "client"
    assert reclassify_client({"client_kind": "robot"}) == "robot"


def test_reclassify_row_unknown_when_no_raw_and_no_stored_kind() -> None:
    assert reclassify_client({}) == "unknown"
    assert reclassify_client({"client_kind": "bogus"}) == "unknown"


def test_reclassify_row_result_is_always_a_closed_set_member() -> None:
    for row in [
        {"client": "curl/8"},
        {"user_agent": "claude-code/1"},
        {"client_kind": "unknown"},
        {},
    ]:
        assert reclassify_client(row) in CLIENT_KINDS


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
