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
