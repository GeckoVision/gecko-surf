"""Per-op mode override on ``McpSurface`` — the catalog-not-the-relay seam.

``recorded_ops`` forces a named tool to stay RECORDED even on a live surface (a
money-moving write we catalog but must never relay). Proven with a light fake client
that records the mode each ``call`` ran in AND blows up if the money-mover ever takes the
LIVE path — so no network could have happened. Offline throughout (Pattern B).
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.events import set_surf_sink_override
from gecko.mcp_server import McpSurface


class _Anchor:
    """The out-of-band trust anchor a real client carries (pins the trusted host set)."""

    def __init__(
        self, state: str = "pinned", trusted_hosts: frozenset[str] = frozenset()
    ):
        self.state = state
        self.trusted_hosts = trusted_hosts


class _TwoOpClient:
    """Fake ``AgentApiClient`` with one read op and one write op. Records the mode each
    ``call`` ran in; a LIVE call to ``write_op`` means the override failed and the wire
    was about to be hit, so it raises — the test can never silently pass."""

    surface_id = "fake-surface"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.anchor = _Anchor()
        self.operations: list[Any] = []

    @staticmethod
    def _tool(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "description": name,
            "inputSchema": {"type": "object", "properties": {}},
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [self._tool("read_op"), self._tool("write_op")]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []

    def call(
        self, name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        if name == "write_op" and mode == "live":  # pragma: no cover - only on a bug
            raise AssertionError("money-mover reached the LIVE path (override failed)")
        self.calls.append((name, mode))
        return {"status": 200, "mode": mode, "data": {}}


@pytest.fixture(autouse=True)
def _silence_events():
    set_surf_sink_override(lambda doc: None)
    try:
        yield
    finally:
        set_surf_sink_override(None)


def test_recorded_op_stays_recorded_on_a_live_surface() -> None:
    client = _TwoOpClient()
    surface = McpSurface(
        client,  # type: ignore[arg-type]
        mode="live",
        enforce="off",
        recorded_ops=frozenset({"write_op"}),
    )

    out = surface.call_tool("write_op", {})

    assert out["mode"] == "recorded"  # synthesized, not relayed
    assert client.calls == [("write_op", "recorded")]  # live path NOT taken


def test_read_op_goes_live_on_the_same_surface() -> None:
    client = _TwoOpClient()
    surface = McpSurface(
        client,  # type: ignore[arg-type]
        mode="live",
        enforce="off",
        recorded_ops=frozenset({"write_op"}),
    )

    out = surface.call_tool("read_op", {})

    assert out["mode"] == "live"  # wire path taken
    assert client.calls == [("read_op", "live")]


def test_empty_recorded_ops_is_byte_identical_recorded_surface() -> None:
    # Default empty set -> every op uses the surface mode (today's behavior).
    client = _TwoOpClient()
    surface = McpSurface(client, mode="recorded", enforce="off")  # type: ignore[arg-type]

    out = surface.call_tool("read_op", {})

    assert out["mode"] == "recorded"
    assert client.calls == [("read_op", "recorded")]


def test_empty_recorded_ops_live_surface_calls_everything_live() -> None:
    # Default empty set on a live surface -> reads AND writes go live (unchanged).
    client = _TwoOpClient()
    surface = McpSurface(client, mode="live", enforce="off")  # type: ignore[arg-type]

    out = surface.call_tool("read_op", {})

    assert out["mode"] == "live"
    assert client.calls == [("read_op", "live")]
