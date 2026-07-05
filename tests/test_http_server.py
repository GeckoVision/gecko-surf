"""Offline Streamable-HTTP E2E: mount the ASGI app in-process (httpx ASGITransport,
no socket, no real network), connect with the real mcp streamable-http client, and
prove the EXISTING comprehension surface reaches an agent first-call-correct.

Also the M1 control-plane test: the server hands the payload back in the JSON-RPC
reply but persists/logs NOTHING but redacted correctness metadata.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

mcp = pytest.importorskip("mcp")  # skip cleanly if the serve extra isn't installed

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko.http_server import build_http_app  # noqa: E402
from gecko.mcp_server import McpSurface  # noqa: E402

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")
BASE = "http://test"
ALLOWED_HOST = "test"

SENTINEL = "SECRET-PAYLOAD-sk-live-DO-NOT-LOG"


def _app(spec_or_surface: Any = PEGANA, mode: str = "recorded") -> Any:
    return build_http_app(
        spec_or_surface,
        mode=mode,
        allowed_hosts=[ALLOWED_HOST],
        allowed_origins=[BASE],
    )


async def _connect(app: Any, fn: Any) -> Any:
    # Run the app lifespan so the streamable-http session manager is live in-process,
    # and drive it with an httpx ASGITransport client — no socket, no real network.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE
        ) as http_client:
            async with streamable_http_client(
                f"{BASE}/mcp", http_client=http_client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)


def _list_tool_specs(app: Any) -> list[Any]:
    async def body(session: ClientSession) -> list[Any]:
        res = await session.list_tools()
        return list(res.tools)

    return anyio.run(_connect, app, body)


def _call(app: Any, name: str, args: dict[str, Any]) -> str:
    async def body(session: ClientSession) -> str:
        res = await session.call_tool(name, args)
        return res.content[0].text  # type: ignore[union-attr]

    return anyio.run(_connect, app, body)


# --- list_tools: surface reaches the agent, auth hidden ---


def test_list_tools_exposes_search_and_question_shaped_tools():
    tools = _list_tool_specs(_app())
    names = {t.name for t in tools}
    assert "search_capabilities" in names  # synthetic intent->endpoint tool
    assert "state" in names  # a question-shaped pegana tool
    # auth-gated ops are hidden from a no-auth (public) session
    assert "me" not in names
    assert "create_sub" not in names


def test_no_tool_leaks_auth_headers():
    tools = _list_tool_specs(_app())
    auth_names = {"authorization", "x-api-token", "x-api-key", "api-key", "x-apikey"}
    for t in tools:
        props = (t.inputSchema or {}).get("properties", {})
        assert not (auth_names & {p.lower() for p in props}), t.name


# --- first-call-correct over the wire (recorded, $0) ---


def test_recorded_call_is_first_call_correct():
    # agent supplies the meaningful input; the path param lands in the right slot.
    raw = _call(_app(), "state", {"symbol": "USDC"})
    result = json.loads(raw)
    assert result["status"] == 200
    assert result["mode"] == "recorded"
    assert result["method"] == "GET"
    assert result["request"].endswith("/v1/assets/USDC/state")


def test_search_capabilities_round_trips():
    raw = _call(_app(), "search_capabilities", {"query": "peg state for an asset"})
    hits = json.loads(raw)
    assert isinstance(hits, list) and hits
    assert all("name" in h for h in hits)


# --- control plane: payload returned, never persisted or logged ---


class _SentinelClient:
    """A light fake whose response carries a secret; the server must not log it."""

    # The MCP surface emits a control-plane-safe ``surf.call`` event keyed by the
    # client's opaque surface id (#39 usage instrumentation); a real AgentApiClient
    # always carries one, so the fake must too.
    surface_id = "sentinel"

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_thing",
                "description": "Get the thing.",
                "inputSchema": {"type": "object", "properties": {}},
                "requires_auth": False,
                "auth_schemes": [],
                "_invoke": {"method": "GET", "path": "/thing", "param_locations": {}},
            }
        ]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return [
            {"name": "get_thing", "summary": "thing", "path": "/thing", "method": "GET"}
        ]

    def call(
        self, name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        return {
            "status": 200,
            "request": "https://api.example.com/thing",
            "method": "GET",
            "data": {"secret": SENTINEL},
            "mode": mode,
        }


def test_server_returns_payload_but_logs_no_payload(caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # any stray persistence would land here
    surface = McpSurface(_SentinelClient(), mode="recorded")  # type: ignore[arg-type]
    app = _app(surface)

    with caplog.at_level(logging.DEBUG):
        raw = _call(app, "get_thing", {})

    # the agent DID receive the payload in the JSON-RPC reply...
    assert SENTINEL in raw

    # ...but Gecko's own logs carry ONLY redacted correctness metadata.
    gecko_logs = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("gecko")
    )
    assert SENTINEL not in gecko_logs
    assert "call tool=get_thing status=200 ok=True" in gecko_logs

    # and nothing was persisted to disk.
    assert list(tmp_path.iterdir()) == []


# --- Phase-0 corpus capture: opt-in, off by default, metadata-only ---


def test_no_corpus_written_when_capture_disabled(tmp_path, monkeypatch):
    # Capture is OFF unless a path is given (the §7-#1 data-path decision stays
    # the founder's to flip); a normal call must persist nothing.
    monkeypatch.chdir(tmp_path)
    _call(_app(), "state", {"symbol": "USDC"})
    assert list(tmp_path.iterdir()) == []


def test_corpus_capture_records_outcome_when_enabled(tmp_path):
    from gecko.corpus import ALLOWED_KEYS, synthetic_sibling

    corpus = tmp_path / "corpus.jsonl"
    app = build_http_app(
        PEGANA,
        mode="recorded",
        allowed_hosts=[ALLOWED_HOST],
        allowed_origins=[BASE],
        corpus_path=str(corpus),
    )
    raw = _call(app, "state", {"symbol": "USDC"})
    assert json.loads(raw)["status"] == 200

    # Serving in recorded mode fabricates the 200 -> synthetic -> segregated file. The
    # main corpus stays empty so a reader never counts a served-recorded call as real.
    synthetic = synthetic_sibling(corpus)
    assert not corpus.exists()
    lines = synthetic.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == ALLOWED_KEYS  # allowlist, full record
    assert rec["operation_id"] == "state"
    assert rec["status"] == 200 and rec["ok"] is True
    assert rec["first_call_correct"] is True  # record-level; excluded at aggregate
    assert rec["source"] == "synthetic"
    assert (
        "{" in rec["path_template"]
    )  # templated, not the filled /v1/assets/USDC/state
    assert "USDC" not in synthetic.read_text()  # no param VALUE leaks


# --- funnel telemetry: the initialize handshake emits surf.connect ---


def _sink_capture():
    """Inject a fake surf-event sink and return the captured docs list."""
    from gecko import events

    docs: list[dict[str, Any]] = []
    events.set_surf_sink_override(lambda d: docs.append(dict(d)))
    return docs


def test_initialize_emits_connect_and_normal_session_still_works(monkeypatch):
    # The hook must fire surf.connect on a real handshake AND leave the transport
    # working (list_tools still round-trips through the same wrapped /mcp app).
    from gecko import events

    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")  # arm the sink path
    docs = _sink_capture()
    try:
        tools = _list_tool_specs(_app())
    finally:
        events.set_surf_sink_override(None)

    names = {t.name for t in tools}
    assert "search_capabilities" in names  # transport unbroken by the wrapper

    connects = [d for d in docs if d["event"] == "surf.connect"]
    assert len(connects) == 1
    conn = connects[0]
    # The mcp client's clientInfo reaches us as a sanitized label...
    assert isinstance(conn.get("client"), str) and conn["client"]
    # ...and the transport-assigned session id is captured for connect<->call joins.
    assert isinstance(conn.get("session_id"), str) and conn["session_id"]
    assert set(conn) <= events.RECORD_ALLOWED_KEYS


def test_call_carries_the_same_session_id_as_connect(monkeypatch):
    # Retention: a surf.call must carry the SAME session id as its surf.connect, so
    # the funnel can join connect->call per session.
    from gecko import events

    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    docs = _sink_capture()
    try:
        raw = _call(_app(), "state", {"symbol": "USDC"})
    finally:
        events.set_surf_sink_override(None)
    assert json.loads(raw)["status"] == 200

    connect = next(d for d in docs if d["event"] == "surf.connect")
    call = next(d for d in docs if d["event"] == "surf.call")
    assert call["session_id"] == connect["session_id"]
    assert call["session_id"]  # non-empty -> retention join is real, not aggregate


def test_search_capabilities_is_not_recorded(tmp_path):
    # The synthetic intent tool is not an upstream API call — it must not pollute
    # the per-operation correctness corpus.
    corpus = tmp_path / "corpus.jsonl"
    app = build_http_app(
        PEGANA,
        allowed_hosts=[ALLOWED_HOST],
        allowed_origins=[BASE],
        corpus_path=str(corpus),
    )
    _call(app, "search_capabilities", {"query": "peg state for an asset"})
    assert not corpus.exists() or corpus.read_text().strip() == ""


def test_capture_records_metadata_never_the_payload(tmp_path):
    # The strongest control-plane proof: real proxy path, body returned to the
    # agent, but the corpus file carries ONLY allowlisted metadata.
    corpus = tmp_path / "corpus.jsonl"
    surface = McpSurface(_SentinelClient(), mode="recorded")  # type: ignore[arg-type]
    app = build_http_app(
        surface,
        allowed_hosts=[ALLOWED_HOST],
        allowed_origins=[BASE],
        corpus_path=str(corpus),
    )
    from gecko.corpus import synthetic_sibling

    raw = _call(app, "get_thing", {})
    assert SENTINEL in raw  # agent receives the payload...
    # Default serving mode is recorded -> synthetic -> segregated file; either way the
    # control-plane proof is the same: the corpus carries ONLY allowlisted metadata.
    body = synthetic_sibling(corpus).read_text()
    assert SENTINEL not in body  # ...but the corpus never does
    rec = json.loads(body.strip())
    assert rec["operation_id"] == "get_thing" and rec["ok"] is True
    assert rec["source"] == "synthetic"
