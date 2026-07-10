"""The bundled Jupiter Swap example must load from package data and comprehend offline —
so `uvx --from "gecko-surf[serve]" jupiter-mcp` works with no local file and no network.
Jupiter's Swap API is keyless by default; an optional JUPITER_API_KEY unlocks the Pro host."""

import json
from typing import Any

from gecko.examples.jupiter import (
    BASE_KEYLESS,
    BASE_PRO,
    _verify_key,
    build_client,
    load_spec,
)


class _FakeClient:
    """A stand-in for AgentApiClient to test the Pro-key self-check offline."""

    def __init__(
        self,
        status: Any = 200,
        tool: str | None = "ProgramIdToLabelGet",
        raise_exc: bool = False,
    ):
        self._status, self._tool, self._raise = status, tool, raise_exc

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": self._tool}] if self._tool else [{"name": "QuoteGet"}]

    def call(
        self, tool_name: str, args: dict[str, Any], mode: str = "recorded"
    ) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("network down")
        return {"status": self._status, "data": {}, "mode": mode}


def test_verify_key_serves_keyless_without_a_key():
    ok, msg = _verify_key(_FakeClient(), has_key=False)
    assert ok is True and "keyless" in msg.lower()


def test_verify_key_aborts_on_invalid_pro_key():
    ok, msg = _verify_key(_FakeClient(status=401), has_key=True)
    assert ok is False and "invalid" in msg.lower()


def test_verify_key_passes_on_200_with_key():
    ok, msg = _verify_key(_FakeClient(status=200), has_key=True)
    assert ok is True and "verified" in msg.lower()


def test_verify_key_fails_open_on_network_error():
    # a transient error must never block serving — only a real 401/403 aborts.
    ok, _ = _verify_key(_FakeClient(raise_exc=True), has_key=True)
    assert ok is True


def test_packaged_spec_loads_from_package_data():
    spec = load_spec()
    assert len(spec["paths"]) == 4
    # the four documented Swap API routes, not display labels.
    assert set(spec["paths"]) == {
        "/quote",
        "/swap",
        "/swap-instructions",
        "/program-id-to-label",
    }


def test_bundled_surface_comprehends_to_four_tools_offline():
    # No network, no key — comprehension only, the free-tier default.
    client = build_client()
    tools = client.list_tools()
    assert len(tools) == 4
    assert {t["name"] for t in tools} == {
        "QuoteGet",
        "SwapPost",
        "SwapInstructionsPost",
        "ProgramIdToLabelGet",
    }


def test_keyless_default_targets_lite_api_and_needs_no_auth():
    # The free tier serves against lite-api.jup.ag with a no-auth session; because the
    # ops carry no securityScheme, all four stay visible to the agent.
    client = build_client()
    assert client.base_url == BASE_KEYLESS
    assert not any(t.get("requires_auth") for t in client.list_tools())


def test_pro_key_injected_at_call_time_and_hidden_from_tool_defs(monkeypatch):
    # invariant #4: the Pro key is injected only at call time, never in the tool defs
    # handed to the agent, and only to Jupiter's pinned (api.jup.ag) host.
    monkeypatch.setenv("JUPITER_API_KEY", "SECRET_PRO_KEY_xyz")
    client = build_client(pro=True)
    assert client.base_url == BASE_PRO

    tools = client.list_tools()
    assert "SECRET_PRO_KEY_xyz" not in json.dumps(tools)

    req = client.prepare(
        "QuoteGet",
        {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "amount": 100000000,
        },
    )
    headers = {k.lower(): v for k, v in req.headers.items()}
    assert headers.get("x-api-key") == "SECRET_PRO_KEY_xyz"


def test_console_entry_networking_flags_mirror_gecko_serve():
    """Loopback-only bind broke sandboxed harnesses whose MCP client doesn't share the
    shell's network namespace — the console entry carries the same four flags."""
    from gecko.examples.jupiter import _mcp_url, _parse_args

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
    """serve_http is imported lazily inside jupiter.main() from gecko.http_server —
    patching the attribute on that source module is what takes effect; this fake just
    returns instead of blocking on a real uvicorn server."""


def test_main_falls_back_to_bundled_when_registry_unreachable(monkeypatch, capsys):
    import gecko.registry.client as registry_client

    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.setattr("gecko.http_server.serve_http", _no_serve)

    def _raise(*a: Any, **k: Any) -> Any:
        raise OSError("network unreachable")

    monkeypatch.setattr(registry_client, "fetch_surface", _raise)

    from gecko.examples.jupiter import main

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "surface source: bundled" in out
    assert "Jupiter Swap — 4 first-call-correct tools ready." in out


def test_main_prefers_registry_when_reachable(monkeypatch, capsys):
    import gecko.registry.client as registry_client
    from gecko.registry.client import FetchedSurface

    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.setattr("gecko.http_server.serve_http", _no_serve)

    bundled_spec = load_spec()
    monkeypatch.setattr(
        registry_client,
        "fetch_surface",
        lambda *a, **k: FetchedSurface(
            name="jupiter", surface_rev="deadbeef00", tier="free", spec=bundled_spec
        ),
    )

    from gecko.examples.jupiter import main

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "surface source: registry rev deadbeef" in out
