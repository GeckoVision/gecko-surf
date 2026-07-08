"""The bundled Colosseum example must load from package data and comprehend offline —
so `uvx --from "gecko-surf[serve]" colosseum-mcp` works with no local file and no network."""

import json
from typing import Any

from gecko.examples.colosseum import _verify_pat, build_client, load_spec


class _FakeClient:
    """A stand-in for AgentApiClient to test the PAT self-check offline."""

    def __init__(
        self, status: Any = 200, tool: str | None = "getStatus", raise_exc: bool = False
    ):
        self._status, self._tool, self._raise = status, tool, raise_exc

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": self._tool}] if self._tool else [{"name": "listGrants"}]

    def call(
        self, tool_name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("network down")
        return {"status": self._status, "data": {}, "mode": mode}


def test_verify_pat_aborts_on_expired_token():
    ok, msg = _verify_pat(_FakeClient(status=401))
    assert ok is False and "invalid or expired" in msg


def test_verify_pat_passes_on_200():
    ok, msg = _verify_pat(_FakeClient(status=200))
    assert ok is True and "verified" in msg.lower()


def test_verify_pat_fails_open_on_network_error():
    # a transient error must never block serving — only a real 401/403 aborts.
    ok, _ = _verify_pat(_FakeClient(raise_exc=True))
    assert ok is True


def test_verify_pat_skips_when_no_status_endpoint():
    ok, msg = _verify_pat(_FakeClient(tool=None))
    assert ok is True and "no status endpoint" in msg


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


def _no_serve(*a: Any, **k: Any) -> None:
    """serve_http is imported lazily inside colosseum.main() from gecko.http_server —
    patching the attribute on that source module is what takes effect; this fake
    just returns instead of blocking on a real uvicorn server."""


def test_main_falls_back_to_bundled_when_registry_unreachable(monkeypatch, capsys):
    import gecko.registry.client as registry_client

    monkeypatch.setenv("COLOSSEUM_COPILOT_PAT", "test-token-xyz")
    monkeypatch.setattr("gecko.http_server.serve_http", _no_serve)
    # keep the startup PAT probe offline + deterministic (its own unit tests cover it)
    monkeypatch.setattr("gecko.examples.colosseum._verify_pat", lambda c: (True, "ok"))

    def _raise(*a: Any, **k: Any) -> Any:
        raise OSError("network unreachable")

    monkeypatch.setattr(registry_client, "fetch_surface", _raise)

    from gecko.examples.colosseum import main

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "surface source: bundled" in out


def test_main_prefers_registry_when_reachable(monkeypatch, capsys):
    import gecko.registry.client as registry_client
    from gecko.registry.client import FetchedSurface

    monkeypatch.setenv("COLOSSEUM_COPILOT_PAT", "test-token-xyz")
    monkeypatch.setattr("gecko.http_server.serve_http", _no_serve)
    monkeypatch.setattr("gecko.examples.colosseum._verify_pat", lambda c: (True, "ok"))

    bundled_spec = load_spec()
    monkeypatch.setattr(
        registry_client,
        "fetch_surface",
        lambda *a, **k: FetchedSurface(
            name="colosseum", surface_rev="deadbeef00", tier="free", spec=bundled_spec
        ),
    )

    from gecko.examples.colosseum import main

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "surface source: registry rev deadbeef" in out
