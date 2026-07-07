"""The bundled Colosseum example must load from package data and comprehend offline —
so `uvx --from "gecko-surf[serve]" colosseum-mcp` works with no local file and no network."""

import json

from gecko.examples.colosseum import build_client, load_spec


def test_packaged_spec_loads_from_package_data():
    spec = load_spec()
    assert len(spec["paths"]) == 11
    # the doc-label trap fix must be baked in: real routes, not the display labels.
    assert "/status" in spec["paths"]
    assert "/colosseum_copilot/status" not in spec["paths"]


def test_bundled_surface_comprehends_and_hides_auth_offline():
    # No network, no real PAT — comprehension only.
    client = build_client("test-token-xyz")
    tools = client.list_tools()
    assert len(tools) == 11
    # invariant #4: the token never appears in the tool defs handed to the agent.
    assert "test-token-xyz" not in json.dumps(tools)


def test_analyze_and_compare_carry_the_documented_cohort_schema():
    """Regression: the stub shipped a wrong /analyze shape (query+free-form cohort);
    a real agent's first call got INVALID_QUERY. The documented shape is
    cohort+dimensions (live-verified 2026-07-07)."""
    client = build_client("test-token-xyz")
    tools = {t["name"]: t for t in client.list_tools()}

    analyze = tools["analyzeCohort"]["inputSchema"]["properties"]["body"]
    assert set(analyze["required"]) == {"cohort", "dimensions"}
    assert "query" not in analyze["properties"]
    # the shared Cohort definition must be resolved into the tool (agents see fields,
    # not a free-form object they have to guess at).
    cohort_props = analyze["properties"]["cohort"]["properties"]
    assert {"hackathons", "winnersOnly", "clusterKeys"} <= set(cohort_props)
    assert cohort_props["prizePlacements"]["items"]["type"] == "integer"

    compare = tools["compareProjects"]["inputSchema"]["properties"]["body"]
    assert set(compare["required"]) == {"cohortA", "cohortB", "dimensions"}

    feedback = tools["submitFeedback"]["inputSchema"]["properties"]["body"]
    assert set(feedback["required"]) == {"category", "message"}
    suggest = tools["sourceSuggestions"]["inputSchema"]["properties"]["body"]
    assert suggest["required"] == ["url"]


def test_console_entry_networking_flags_mirror_gecko_serve():
    """Regression: loopback-only bind broke sandboxed harnesses whose MCP client
    doesn't share the shell's network namespace (co-founder field report, 2026-07-07)."""
    from gecko.examples.colosseum import _mcp_url, _parse_args

    args = _parse_args([])
    assert (args.host, args.port, args.public_url, args.allow_host) == (
        "127.0.0.1",
        8000,
        None,
        [],
    )
    args = _parse_args(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--allow-host",
            "gecko.example.com:9000",
            "--public-url",
            "https://t.trycloudflare.com",
        ]
    )
    assert args.host == "0.0.0.0" and args.port == 9000
    assert args.allow_host == ["gecko.example.com:9000"]
    assert _mcp_url(args.host, args.port, args.public_url) == (
        "https://t.trycloudflare.com/mcp"
    )
    assert _mcp_url("127.0.0.1", 8000, None) == "http://127.0.0.1:8000/mcp"
