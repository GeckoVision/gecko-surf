"""The legacy HTTP+SSE transport, mounted alongside Streamable HTTP.

Not nostalgia — distribution. The hosted chat products (Grok, ChatGPT, Claude on the web)
accept a remote MCP server through the two-endpoint SSE shape, so without it Gecko cannot
be added inside the window where people already are.

These assert the SHAPE — both doors exist, both halves of the two-endpoint transport are
mounted, and losing the extra door never closes the main one. They deliberately do not
open a live stream: an SSE connection stays open by design, so a test that reads one
blocks forever. Proving the stream carries real MCP traffic is an integration concern,
and the honest place for it is a client actually connecting.
"""

from __future__ import annotations

from typing import Any

from gecko import http_server


def _app() -> Any:
    from gecko.client import AgentApiClient
    from gecko.ingest import load_spec

    spec = load_spec("gecko/examples/jupiter_swap_openapi.json")
    return http_server.build_http_app(AgentApiClient(spec))


def _paths(app: Any) -> set[str]:
    return {(getattr(r, "path", "") or "").rstrip("/") for r in app.routes}


def test_streamable_http_is_still_the_default_door() -> None:
    """SSE is ADDITIONAL. Replacing the modern transport to please an old client would
    trade one compatibility problem for another."""
    assert http_server.MCP_PATH.rstrip("/") in _paths(_app())


def test_the_sse_stream_endpoint_is_mounted() -> None:
    assert http_server.SSE_PATH.rstrip("/") in _paths(_app())


def test_the_client_half_is_mounted_too() -> None:
    """SSE is two-endpoint: the stream comes down `/sse`, the client's own messages go up
    to the message path. Mounting only the stream looks right and never works."""
    assert http_server.SSE_MESSAGE_PATH.rstrip("/") in _paths(_app())


def test_the_two_sse_routes_come_as_a_pair() -> None:
    from unittest.mock import Mock

    routes = http_server._sse_routes(Mock(), None)

    assert len(routes) == 2
    assert {(getattr(r, "path", "") or "").rstrip("/") for r in routes} == {
        http_server.SSE_PATH.rstrip("/"),
        http_server.SSE_MESSAGE_PATH.rstrip("/"),
    }


def test_losing_the_extra_door_never_closes_the_main_one(monkeypatch: Any) -> None:
    """An `mcp` without SseServerTransport must still boot the server on Streamable
    HTTP — an optional transport that can take the process down is not optional."""
    monkeypatch.setattr(http_server, "_sse_routes", lambda *_a, **_kw: [])

    paths = _paths(_app())

    assert http_server.MCP_PATH.rstrip("/") in paths
    assert http_server.SSE_PATH.rstrip("/") not in paths


def _multi_app() -> Any:
    """The hosted shape: surfaces mounted under one host, which is what
    mcp.geckovision.tech actually serves — the per-surface app is only ever a mount."""
    from gecko.client import AgentApiClient
    from gecko.ingest import load_spec

    spec = load_spec("gecko/examples/jupiter_swap_openapi.json")
    return http_server.build_multi_surface_app([("jupiter", AgentApiClient(spec))])


def test_the_root_sse_url_points_somewhere() -> None:
    """A hosted chat product asks for "the SSE endpoint" and the person pastes the host.
    Bare /sse must not dead-end — that 404 is a silent onboarding failure, the same one
    the bare /mcp alias already exists to prevent."""
    from starlette.testclient import TestClient

    response = TestClient(_multi_app()).get(
        http_server.SSE_PATH, follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"].endswith(
        f"/{http_server.META_SURFACE_NAME}{http_server.SSE_PATH}"
    )


def test_the_alias_never_replaces_a_surfaces_own_door() -> None:
    """The alias is a convenience, not the transport. Each surface stays mounted, and
    the meta surface the alias points at has to actually be there."""
    mounted = {(getattr(r, "path", "") or "").rstrip("/") for r in _multi_app().routes}

    assert "/jupiter" in mounted
    assert f"/{http_server.META_SURFACE_NAME}" in mounted


def test_a_stuck_client_cannot_pin_every_connection_slot(monkeypatch: Any) -> None:
    """The cap is per REAL client IP. Held open, SSE is a socket per client — the failure
    we bound here is usually accidental (one client that never disconnects), not hostile."""
    monkeypatch.setenv(http_server.SSE_MAX_STREAMS_PER_IP_ENV, "2")

    assert http_server._sse_limit(http_server.SSE_MAX_STREAMS_PER_IP_ENV, 4) == 2


def test_a_typo_in_the_deploy_env_never_disables_a_cap(monkeypatch: Any) -> None:
    """`GECKO_SSE_MAX_STREAMS_PER_IP=four` must fall back to the default, not to
    unlimited — a limit that silently turns itself off is worse than no limit."""
    monkeypatch.setenv(http_server.SSE_MAX_STREAMS_PER_IP_ENV, "four")

    assert http_server._sse_limit(http_server.SSE_MAX_STREAMS_PER_IP_ENV, 4) == 4


def test_a_cap_can_be_turned_off_deliberately(monkeypatch: Any) -> None:
    """0 means unlimited, for local dev. Explicit, unlike the typo above."""
    monkeypatch.setenv(http_server.SSE_MAX_STREAM_SECONDS_ENV, "0")

    assert http_server._sse_limit(http_server.SSE_MAX_STREAM_SECONDS_ENV, 1800) == 0


def test_a_negative_cap_is_clamped_not_inverted(monkeypatch: Any) -> None:
    monkeypatch.setenv(http_server.SSE_MAX_STREAMS_PER_IP_ENV, "-5")

    assert http_server._sse_limit(http_server.SSE_MAX_STREAMS_PER_IP_ENV, 4) == 0


def test_a_client_at_its_cap_is_refused() -> None:
    """The cap exists because SSE holds a socket per client. The failure it bounds is
    usually accidental — one client that never disconnects — not hostile."""
    live: dict[str, int] = {}

    taken = [http_server._sse_try_acquire(live, "1.2.3.4", 2) for _ in range(3)]

    assert taken == [True, True, False]
    assert live["1.2.3.4"] == 2


def test_one_clients_cap_never_blocks_another() -> None:
    """Behind the ALB this only holds because uvicorn resolves X-Forwarded-For into
    client.host — otherwise every caller shares one address and one heavy user would
    lock out the world."""
    live: dict[str, int] = {}
    http_server._sse_try_acquire(live, "1.1.1.1", 1)

    assert http_server._sse_try_acquire(live, "2.2.2.2", 1) is True


def test_a_vanished_client_gives_its_slot_back() -> None:
    """A leaked count starves that IP into permanent 429s — the outage would arrive
    hours later, looking nothing like its cause."""
    live: dict[str, int] = {}
    http_server._sse_try_acquire(live, "1.2.3.4", 2)
    http_server._sse_release(live, "1.2.3.4")

    assert http_server._sse_try_acquire(live, "1.2.3.4", 2) is True


def test_the_slot_map_cannot_grow_without_bound() -> None:
    """Every distinct IP would otherwise leave a permanent key behind — a slow leak on
    a public endpoint that sees thousands of crawler IPs."""
    live: dict[str, int] = {}
    http_server._sse_try_acquire(live, "1.2.3.4", 2)
    http_server._sse_release(live, "1.2.3.4")

    assert live == {}


def test_a_zero_cap_lets_everything_through() -> None:
    live: dict[str, int] = {}

    assert all(http_server._sse_try_acquire(live, "1.2.3.4", 0) for _ in range(50))


def test_both_sse_doors_are_gated_when_a_gate_is_wired() -> None:
    """SSE is TWO endpoints, so gating one of them gates nothing.

    The stream is opened with `GET /sse`; every request the client then makes goes to
    `POST /messages/?session_id=...`. Gating only the GET leaves the session id in a query
    string as the sole authorization for every subsequent call — a capability URL, on a
    surface whose whole point is that a key is required. The SDK has its own owner check,
    but it compares `scope["user"]`, which this gate never sets, so both sides are None
    and it always passes.
    """
    from unittest.mock import Mock

    from starlette.routing import Mount, Route

    routes = http_server._sse_routes(Mock(), Mock())

    for route in routes:
        app = (
            route.app if isinstance(route, Mount) else getattr(route, "endpoint", None)
        )
        # Starlette wraps a Route's endpoint, so reach for the gate on either shape.
        gated = isinstance(app, http_server._GeckoKeyGateASGI) or isinstance(
            getattr(route, "endpoint", None), http_server._GeckoKeyGateASGI
        )
        assert gated, (
            f"{getattr(route, 'path', route)} is reachable without the gecko key; "
            "both halves of the SSE transport must be gated or neither is"
        )
    assert isinstance(routes[0], Route) and isinstance(routes[1], Mount)
