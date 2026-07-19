from pathlib import Path

from gecko.client import AgentApiClient
from gecko.mcp_server import McpSurface

FIXTURE = Path(__file__).parent / "fixtures" / "txodds_docs.yaml"


def _client() -> AgentApiClient:
    return AgentApiClient(str(FIXTURE))


def _odds_tool_name(client: AgentApiClient) -> str:
    for t in client.list_tools():
        inv = t["_invoke"]
        if inv["path"] == "/api/odds/snapshot/{fixtureId}" and inv["method"] == "GET":
            return t["name"]
    raise AssertionError("odds snapshot tool not found")


def test_client_derives_base_url_from_spec():
    assert _client().base_url == "https://txline.txodds.com"


def test_client_search_then_call_recorded():
    client = _client()
    hits = client.search("live odds for a fixture")
    assert hits and "odds" in hits[0]["path"]
    name = _odds_tool_name(client)
    result = client.call(name, {"fixtureId": 4242}, mode="recorded")
    assert result["status"] == 200
    assert result["mode"] == "recorded"
    assert "/api/odds/snapshot/4242" in result["request"]
    assert result["data"] is not None


def test_prepare_injects_session_auth():
    # Auth injects only toward an out-of-band-pinned anchor. A local file is NOT a
    # pinning provenance (Fix #1) — the anchor must come from an explicit base_url.
    client = AgentApiClient(str(FIXTURE), base_url="https://txline.txodds.com")
    req = client.prepare(_odds_tool_name(client), {"fixtureId": 1})
    assert req.headers["Authorization"].startswith("Bearer ")
    assert "X-Api-Token" in req.headers


def test_mcp_surface_lists_search_plus_endpoints():
    surface = McpSurface(_client())
    tools = surface.list_tools()
    assert tools[0]["name"] == "search_capabilities"
    assert tools[1]["name"] == "query_docs"  # the self-heal tool must be discoverable
    assert len(tools) == 20  # 2 synthetic tools (search + query_docs) + 18 endpoints


def test_mcp_surface_search_and_call():
    surface = McpSurface(_client())
    found = surface.call_tool("search_capabilities", {"query": "match score updates"})
    assert any("scores" in f["path"] for f in found)


def test_recorded_call_is_synthetic_source_and_segregates_corpus(tmp_path):
    # A recorded-mode call fabricates a 200 — it must be labeled `synthetic`, NOT
    # counted as observed. The emitted FCC event carries source="synthetic" and the
    # corpus record routes to the segregated synthetic.jsonl (never the main corpus),
    # so neither the adoption rate nor the moat metric sees the faked success.
    from gecko import corpus
    from gecko.events import set_surf_sink_override

    events: list[dict] = []
    set_surf_sink_override(lambda doc: events.append(dict(doc)))
    try:
        corpus_path = tmp_path / "corpus.jsonl"
        client = AgentApiClient(str(FIXTURE), corpus_path=corpus_path)
        client.call(_odds_tool_name(client), {"fixtureId": 4242}, mode="recorded")
    finally:
        set_surf_sink_override(None)

    fcc = [e for e in events if e["event"] == "surf.first_call_correct"]
    assert fcc and fcc[-1]["source"] == "synthetic"  # faked 200 is not observed
    assert not corpus_path.exists()  # nothing synthetic in the main corpus
    assert corpus.synthetic_sibling(corpus_path).exists()  # it landed here instead


def test_events_carry_their_plane_so_fcc_vs_call_is_reconcilable():
    # surf.first_call_correct fires at the ENGINE (every client.call — demo, `gecko
    # test`, recorded $0 flows included); surf.call fires only when a tool is invoked
    # THROUGH an MCP surface. They are different planes, not a funnel subset — so
    # all-time fcc > call is expected, and each event must say which plane emitted it.
    from gecko.events import set_surf_sink_override

    events: list[dict] = []
    set_surf_sink_override(lambda doc: events.append(dict(doc)))
    try:
        client = _client()
        name = _odds_tool_name(client)
        # Engine plane: a direct client call (the `gecko test` / demo shape).
        client.call(name, {"fixtureId": 1}, mode="recorded")
        # Surface plane: the same call THROUGH the MCP surface.
        McpSurface(client).call_tool(name, {"fixtureId": 2})
    finally:
        set_surf_sink_override(None)

    fcc = [e for e in events if e["event"] == "surf.first_call_correct"]
    calls = [e for e in events if e["event"] == "surf.call"]
    prepares = [e for e in events if e["event"] == "surf.prepare"]
    assert fcc and all(e["plane"] == "engine" for e in fcc)
    assert prepares and all(e["plane"] == "engine" for e in prepares)
    assert calls and all(e["plane"] == "surface" for e in calls)
    # The surface invocation ALSO resolved at the engine: 2 fcc, 1 surf.call — the
    # exact fcc>call shape the plane field makes queryable.
    assert len(fcc) == 2 and len(calls) == 1


def test_list_tools_emits_one_list_tools_event_with_funnel_fields():
    # The blind connect->call segment: surf.list_tools makes "an agent connected and
    # enumerated the tools" visible, carrying the SAME sanitized correlation fields the
    # other surf events carry (never PII, control-plane only).
    from gecko.events import RECORD_ALLOWED_KEYS, set_surf_sink_override

    events: list[dict] = []
    set_surf_sink_override(lambda doc: events.append(dict(doc)))
    try:
        surface = McpSurface(_client())
        surface.list_tools(
            session_id="sess-abc123",
            user_agent="claude-code/1.9",
            client_kind="client",
        )
    finally:
        set_surf_sink_override(None)

    lt = [e for e in events if e["event"] == "surf.list_tools"]
    assert len(lt) == 1  # exactly once per list_tools call
    doc = lt[0]
    assert doc["session_id"] == "sess-abc123"
    assert doc["user_agent"] == "claude-code/1.9"
    assert doc["client_kind"] == "client"
    assert set(doc) <= RECORD_ALLOWED_KEYS  # nothing off-schema ever leaves


def test_list_tools_is_a_noop_without_a_sink():
    # Ships silent like the other emits: no override + no MONGODB_URI => no phone-home,
    # and list_tools still returns the surface unchanged.
    surface = McpSurface(_client())
    tools = surface.list_tools()  # no sink armed -> emit is a no-op, must not raise
    assert tools[0]["name"] == "search_capabilities"
