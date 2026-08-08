"""The Jito surface must prepare the URL Jito actually serves.

The bug this pins: the hosted surface was built from the docs->draft spec whose paths are
VIRTUAL ``/{method}`` routes (an artifact of modelling JSON-RPC methods as OpenAPI
operations for a *recorded* demo). Mounted LIVE, those went on the wire verbatim —
``POST https://mainnet.block-engine.jito.wtf/getTipAccounts`` — and every read 404'd.

Verified against Jito's own docs (https://docs.jito.wtf/lowlatencytxnsend/) and against
the live host: the real routes are ``/api/v1/{method}`` (with ``/api/v1/bundles`` and
``/api/v1/transactions`` for the two senders), and the tip floor is a plain REST GET on a
DIFFERENT host — ``https://bundles.jito.wtf/api/v1/bundles/tip_floor``.

All offline: an injected transport records the URL the engine WOULD call, so the wire
contract is falsifiable at $0.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.caller import PreparedRequest
from gecko.jito_surface import (
    JITO_MAINNET_BASE,
    JITO_TIPS_BASE,
    build_jito_surface,
    build_jito_tips_surface,
)

# operationId -> the exact path Jito serves it on (docs + live-verified).
WIRE_PATHS = {
    "sendBundle": "/api/v1/bundles",
    "sendTransaction": "/api/v1/transactions",
    "getTipAccounts": "/api/v1/getTipAccounts",
    "getBundleStatuses": "/api/v1/getBundleStatuses",
    "getInflightBundleStatuses": "/api/v1/getInflightBundleStatuses",
}


def _prepared_urls(surface: Any) -> dict[str, str]:
    """The URL each tool would put on the wire (prepare only — nothing is sent)."""
    client = surface.client
    urls = {}
    for tool in client.list_tools():
        name = tool["name"]
        if name in {"search_capabilities", "query_docs", "get_capability"}:
            continue
        urls[name] = client.prepare(name, _min_args(tool), inject_auth=False).url
    return urls


def _min_args(tool: dict[str, Any]) -> dict[str, Any]:
    """Just enough to satisfy required body fields — we only care about the URL."""
    schema = tool["inputSchema"]
    body = schema.get("properties", {}).get("body", {})
    required = body.get("required") or []
    return {"body": {name: "x" for name in required}} if required else {}


@pytest.mark.parametrize("op_id,path", sorted(WIRE_PATHS.items()))
def test_every_block_engine_op_prepares_jitos_real_path(op_id: str, path: str) -> None:
    urls = _prepared_urls(build_jito_surface("off"))

    assert urls[op_id].split("?")[0] == JITO_MAINNET_BASE + path


def test_no_block_engine_op_is_missing_the_api_v1_prefix() -> None:
    for name, url in _prepared_urls(build_jito_surface("off")).items():
        assert "/api/v1/" in url, f"{name} would 404: {url}"


# --- the second host: tip floor is NOT on the block engine --------------------


def test_tip_floor_is_served_from_its_own_pinned_host() -> None:
    surface = build_jito_tips_surface("off")
    urls = _prepared_urls(surface)

    assert urls["getTipFloor"] == JITO_TIPS_BASE + "/api/v1/bundles/tip_floor"
    assert JITO_TIPS_BASE != JITO_MAINNET_BASE  # a different host, by construction


def test_the_block_engine_surface_does_not_claim_the_tip_floor_op() -> None:
    # It cannot be called from the block-engine base (one client pins ONE host), so it
    # must not be advertised there — advertising it is what produced the 404.
    names = {t["name"] for t in build_jito_surface("off").client.list_tools()}

    assert "getTipFloor" not in names


def test_tips_surface_is_read_only_and_keyless() -> None:
    surface = build_jito_tips_surface("block")

    assert surface.mode == "live"
    assert surface.enforce == "block"
    assert not surface.recorded_ops  # nothing to hold back: no money-mover here
    methods = {op.method for op in surface.client.operations}
    assert methods == {"GET"}


# --- both hosts mount the split ----------------------------------------------


def test_serve_mcp_mounts_the_tips_surface_on_its_own_base() -> None:
    from gecko.serve_mcp import _build_surfaces

    surfaces = dict(_build_surfaces("block"))

    assert surfaces["jito"].client.base_url == JITO_MAINNET_BASE
    assert surfaces["jito-tips"].client.base_url == JITO_TIPS_BASE


def test_serve_providers_mounts_the_tips_surface_too() -> None:
    from gecko.serve_providers import _build_surfaces

    surfaces = dict(_build_surfaces())

    assert surfaces["jito"].client.base_url == JITO_MAINNET_BASE
    assert surfaces["jito-tips"].client.base_url == JITO_TIPS_BASE


# --- a wrong path must surface as an ERROR, not as empty data -----------------


def test_a_404_from_the_block_engine_comes_back_flagged() -> None:
    from gecko.toolerror import is_upstream_failure

    def dead(_req: PreparedRequest) -> tuple[int, Any]:
        return 404, ""

    surface = build_jito_surface("off")
    surface.client._live_transport = dead  # the old bug, forced

    out = surface.call_tool("getTipAccounts", _min_args_for(surface, "getTipAccounts"))

    assert out["status"] == 404
    assert is_upstream_failure(out) is True


def _min_args_for(surface: Any, name: str) -> dict[str, Any]:
    tool = next(t for t in surface.client.list_tools() if t["name"] == name)
    return _min_args(tool)
