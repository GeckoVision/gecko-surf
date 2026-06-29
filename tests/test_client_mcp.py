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
    client = _client()
    req = client.prepare(_odds_tool_name(client), {"fixtureId": 1})
    assert req.headers["Authorization"].startswith("Bearer ")
    assert "X-Api-Token" in req.headers


def test_mcp_surface_lists_search_plus_endpoints():
    surface = McpSurface(_client())
    tools = surface.list_tools()
    assert tools[0]["name"] == "search_capabilities"
    assert len(tools) == 19  # 1 search tool + 18 endpoints


def test_mcp_surface_search_and_call():
    surface = McpSurface(_client())
    found = surface.call_tool("search_capabilities", {"query": "match score updates"})
    assert any("scores" in f["path"] for f in found)
