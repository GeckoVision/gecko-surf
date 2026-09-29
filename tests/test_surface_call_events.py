"""Every served surface records its tool calls — not only ``McpSurface``.

Measured 2026-09-28: CloudWatch showed ``POST /course/mcp`` access lines and no
tool-call record at all, because ``surf.call`` was emitted only inside
``McpSurface.call_tool``. The duck-typed surfaces (course, orquestra catalog, meta)
were invisible to the usage report. These tests drive the REAL multi-surface app over
the real MCP client (in-process, no socket) and read what a fake sink received.
"""

from __future__ import annotations

import json
import logging
import logging.config
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

pytest.importorskip("mcp")

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from gecko import events  # noqa: E402
from gecko.http_server import _uvicorn_kwargs, build_multi_surface_app  # noqa: E402
from gecko.providers.course_surface import build_course_surface  # noqa: E402

BASE = "http://test"
PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")
#: An argument value that must never reach an event record.
SENTINEL = "SENTINEL-arg-value-loops-graph-7f3a"


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")  # arm the sink path
    monkeypatch.delenv("GECKO_TELEMETRY", raising=False)
    docs: list[dict[str, Any]] = []
    events.set_surf_sink_override(lambda d: docs.append(dict(d)))
    yield docs
    events.set_surf_sink_override(None)


@pytest.fixture
def course(tmp_path: Path) -> Any:
    page = tmp_path / "units/en/unit1/loops.mdx"
    page.parent.mkdir(parents=True)
    page.write_text(
        "# Loops and graphs\n\nA loop repeats one step until a budget runs out.\n",
        encoding="utf-8",
    )
    built = build_course_surface(tmp_path)
    assert built is not None
    return built


class _FailingSurface:
    """A duck-typed surface whose one tool refuses, the way program surfaces do."""

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": "refuse",
                "description": "always refuses",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]

    def call_tool(self, name: str, arguments: dict[str, Any], **_kw: Any) -> Any:
        return {"error": "simulation-reverted", "detail": SENTINEL}


def _multi(surfaces: list[tuple[str, Any]]) -> Any:
    return build_multi_surface_app(
        surfaces, public_url=BASE, allowed_hosts=["test"], allowed_origins=[BASE]
    )


def _call(app: Any, mount: str, name: str, args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE
            ) as http_client:
                async with streamable_http_client(
                    f"{BASE}/{mount}/mcp", http_client=http_client
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await session.call_tool(name, args)

    return anyio.run(run)


def _calls(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d["event"] == "surf.call"]


def test_course_call_emits_one_surface_event_without_argument_values(
    sink: list[dict[str, Any]], course: Any
) -> None:
    res = _call(
        _multi([("course", course)]), "course", "search_course", {"query": SENTINEL}
    )
    assert not res.isError

    calls = _calls(sink)
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["tool_name"] == "search_course"
    assert call["surface_id"] == "course"  # the mount, same id surf.connect carries
    assert call["plane"] == "surface"
    assert call["ok"] is True
    assert call["error_class"] == "none"
    assert isinstance(call["latency_ms"], int) and call["latency_ms"] >= 0
    connect = next(d for d in sink if d["event"] == "surf.connect")
    assert call["session_id"] == connect["session_id"] and call["session_id"]
    assert set(call) <= events.RECORD_ALLOWED_KEYS
    # The control-plane promise: the argument value appears in NO record.
    assert SENTINEL not in json.dumps(sink)


def test_a_refusing_duck_surface_records_not_ok(sink: list[dict[str, Any]]) -> None:
    res = _call(_multi([("prog", _FailingSurface())]), "prog", "refuse", {})
    assert res.isError

    (call,) = _calls(sink)
    assert call["ok"] is False
    assert call["error_class"] == "other"
    assert SENTINEL not in json.dumps(sink)


def test_mcp_surface_calls_still_emit_exactly_once(sink: list[dict[str, Any]]) -> None:
    res = _call(_multi([("pegana", PEGANA)]), "pegana", "state", {"symbol": "USDC"})
    assert not res.isError
    assert len(_calls(sink)) == 1


# --- the call log line reaches production ---------------------------------------------


def test_server_log_config_keeps_gecko_info_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GECKO_LOG_LEVEL", raising=False)
    config = _uvicorn_kwargs("0.0.0.0", 8000)["log_config"]
    assert config["loggers"]["gecko"]["level"] == "INFO"
    # uvicorn's own loggers are still there: its access log is what we see today.
    assert "uvicorn.access" in config["loggers"]

    names = ("gecko", "uvicorn", "uvicorn.access", "uvicorn.error")
    saved = {
        n: (
            logging.getLogger(n).level,
            logging.getLogger(n).propagate,
            logging.getLogger(n).handlers[:],
        )
        for n in names
    }
    try:
        logging.config.dictConfig(config)
        assert logging.getLogger("gecko.http_server").isEnabledFor(logging.INFO)
    finally:
        for n, (level, propagate, handlers) in saved.items():
            lg = logging.getLogger(n)
            lg.setLevel(level)
            lg.propagate = propagate
            lg.handlers[:] = handlers


def test_server_log_level_is_env_tunable_and_rejects_junk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GECKO_LOG_LEVEL", "warning")
    assert (
        _uvicorn_kwargs("h", 1)["log_config"]["loggers"]["gecko"]["level"] == "WARNING"
    )
    monkeypatch.setenv("GECKO_LOG_LEVEL", "not-a-level")
    assert _uvicorn_kwargs("h", 1)["log_config"]["loggers"]["gecko"]["level"] == "INFO"
