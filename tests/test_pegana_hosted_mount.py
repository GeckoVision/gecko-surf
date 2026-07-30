"""Pegana mounted on the LIVE hosted host (``mcp.geckovision.tech``) as a keyless,
public read surface — mirroring the jupiter mount exactly (a comprehended spec served
LIVE against a pinned base_url, ``public_session`` so no secret can leak, the hosted
risk gate active on it too).

Why this exists: pegana used to live ONLY on ``serve_providers`` (a separate,
undeployed host), so ``mcp.geckovision.tech/pegana/mcp`` returned 404. These tests pin
that pegana is now in the live surface list, keyless/public, guarded by the same risk
gate as the other public mounts, cannot trip the paid-surface boot gate, and mounts a
reachable ``/pegana/mcp`` route.

Fully offline: the surface build is a pure function over the shipped spec; the mount is
exercised with Starlette's in-process ASGI TestClient. No network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko.access import public_session
from gecko.enforce import resolve_hosted_enforce
from gecko.mcp_server import McpSurface

_PEGANA_BASE = "https://api.pegana.xyz"

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


def _hosted_surfaces() -> dict[str, Any]:
    import gecko.serve_mcp as serve_mcp

    return dict(serve_mcp._build_surfaces(hosted_enforce="block"))


# --- pegana is in the live surface list, mirroring the jupiter mount -----------------


def test_pegana_is_in_the_hosted_surface_list() -> None:
    assert "pegana" in _hosted_surfaces()


def test_pegana_is_live_keyless_public_pinned_to_its_host() -> None:
    """Mirror jupiter exactly: an McpSurface built from the comprehended spec, served
    LIVE against the pinned pegana host with a no-auth public session and the hosted
    risk gate active. NO recorded_ops special-casing (unlike jito's relay writes)."""
    pegana = _hosted_surfaces()["pegana"]

    assert isinstance(pegana, McpSurface)
    assert pegana.mode == "live"
    assert pegana.enforce == "block"  # hosted risk gate preserved
    assert pegana.client.base_url == _PEGANA_BASE
    assert pegana.client._base_url_explicit is True
    assert pegana.recorded_ops == frozenset()  # no catalog-only relay carve-out


def test_pegana_session_is_keyless_like_the_other_public_mounts() -> None:
    """Keyless/public: the session injects no auth header, so no key is ever pasted and
    none can leak. Matches jupiter's public_session posture, not birdeye's static key."""
    pegana = _hosted_surfaces()["pegana"]
    reference = public_session()
    assert pegana.client.session.auth_headers() == reference.auth_headers()
    assert pegana.client.session.auth_headers() == {}


def test_pegana_tools_are_auth_hidden() -> None:
    """Invariant #4: no tool def exposes an auth header, and no tool requires auth —
    identical to jupiter's keyless surface."""
    pegana = _hosted_surfaces()["pegana"]
    tools = pegana.client.list_tools()
    blob = json.dumps(tools)
    for shaped in ("authorization", "x-api-key", "apikey", "api_key"):
        assert shaped not in blob.lower()
    assert not any(t.get("requires_auth") for t in tools)


# --- the write ops are guarded by the risk gate, never exposed unguarded -------------


def test_pegana_write_ops_go_through_the_hosted_risk_gate() -> None:
    """Pegana's keyless POST/PATCH/DELETE ops (auth-flow, subs, webhooks) are NOT
    exposed as unguarded writes: with enforce=block the fail-closed boundary refuses an
    unscoreable state-changing call, exactly like every other public mount. This asserts
    the SAFETY POSTURE is wired (a write is gate-eligible), not that any specific op is
    always blocked."""
    from gecko.enforce import WRITE_METHODS
    from gecko.tools import tool_name

    pegana = _hosted_surfaces()["pegana"]
    assert pegana.enforce == "block"
    # There is at least one state-changing op on the surface, so the gate is load-bearing.
    write_ops = [
        op for op in pegana.client.operations if op.method.lower() in WRITE_METHODS
    ]
    assert write_ops, "pegana declares write ops; the gate must cover them"
    # Every write op is gate-eligible under the shared write-op predicate (keyed by the
    # agent-facing tool name, the same value the call-time gate resolves).
    for op in write_ops:
        assert pegana._is_write_op(tool_name(op))  # noqa: SLF001


# --- the boot gate still passes with pegana added (a public mount can't trip it) -----


def test_boot_gate_still_passes_with_pegana_public_and_gate_off() -> None:
    from gecko.serve_mcp import GATED_SURFACES, assert_paid_surfaces_are_gated

    surfaces = list(_hosted_surfaces().items())
    # pegana is public; confirm it is NOT in the gated set.
    assert "pegana" not in GATED_SURFACES
    # A purely-public slice (drop the one declared-paid surface) can never trip the gate.
    public_only = [(n, v) for n, v in surfaces if n not in GATED_SURFACES]
    assert (
        assert_paid_surfaces_are_gated(public_only, GATED_SURFACES, require_key=False)
        is None
    )


# --- /pegana/mcp is a reachable route on the live host app ---------------------------


def test_pegana_mcp_route_mounts_and_initializes() -> None:
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    import gecko.serve_mcp as serve_mcp
    from gecko.http_server import build_multi_surface_app

    surfaces = serve_mcp._build_surfaces(resolve_hosted_enforce("block"))
    app = build_multi_surface_app(
        surfaces,
        allowed_hosts=["testserver"],
        enforce="block",
    )
    with TestClient(app) as client:
        resp = client.post(
            "/pegana/mcp",
            json=_INIT,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert resp.status_code == 200  # keyless: no key needed to initialize
