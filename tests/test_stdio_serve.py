"""Falsifiers for the stdio transport (Phase 1 of zero-friction onboarding).

Offline, $0: ``--stdio`` routes to ``serve_stdio`` with the SAME comprehended client
the HTTP path builds, binds NO port, and — critically — writes NOTHING to stdout
(stdout is the JSON-RPC channel; a stray banner corrupts the protocol stream). Without
``--stdio`` the HTTP path is unchanged, and its banner now leads with the stdio
``claude mcp add … -- … --stdio`` recommendation.
"""

from __future__ import annotations

from pathlib import Path

from gecko import serve
from gecko.client import AgentApiClient
from gecko.examples import colosseum

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")


# --- gecko serve --stdio -----------------------------------------------------


def test_stdio_routes_to_serve_stdio_not_http(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_stdio(client, **kwargs):
        captured["client"] = client
        captured["kwargs"] = kwargs

    def fail_http(*args, **kwargs):
        raise AssertionError("serve_http must not run / bind a port in --stdio mode")

    monkeypatch.setattr(serve, "serve_stdio", fake_stdio)
    monkeypatch.setattr(serve, "serve_http", fail_http)

    rc = serve.main([PEGANA, "--stdio", "--name", "pegana"])

    assert rc == 0
    # The SAME comprehended surface the HTTP path builds, not a spec string.
    assert isinstance(captured["client"], AgentApiClient)
    assert captured["kwargs"]["server_name"] == "pegana"
    assert captured["kwargs"]["mode"] == "recorded"


def test_stdio_keeps_stdout_clean(monkeypatch, capsys) -> None:
    # stdout is the MCP channel — the human note must land on stderr only, or the
    # JSON-RPC stream is corrupted the moment the client connects.
    monkeypatch.setattr(serve, "serve_stdio", lambda *a, **k: None)
    monkeypatch.setattr(
        serve,
        "serve_http",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http in stdio")),
    )

    serve.main([PEGANA, "--stdio", "--name", "pegana"])

    out = capsys.readouterr()
    assert out.out == ""  # protocol-safe: nothing leaked to stdout
    assert "pegana" in out.err  # the startup note went to stderr


def test_http_path_unchanged_without_stdio(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(serve, "serve_http", lambda client, **k: captured.update(k))
    monkeypatch.setattr(
        serve,
        "serve_stdio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stdio must not run")),
    )

    rc = serve.main([PEGANA, "--port", "9123", "--name", "pegana"])

    assert rc == 0
    assert captured["server_name"] == "pegana"
    out = capsys.readouterr().out
    assert "http://127.0.0.1:9123/mcp" in out  # HTTP URL still advertised


def test_http_banner_recommends_stdio(monkeypatch, capsys) -> None:
    monkeypatch.setattr(serve, "serve_http", lambda *a, **k: None)

    serve.main([PEGANA, "--name", "pegana"])

    out = capsys.readouterr().out
    # The stdio add command leads the guidance, and points at the accurate spawn.
    assert f"claude mcp add pegana -- gecko {PEGANA} --stdio" in out
    assert "--stdio" in out
    assert "Recommended" in out
    # HTTP is demoted, not removed.
    assert "remote or shared client" in out.lower()
    # The stale-registration hint now precedes the tunnel.
    assert "claude mcp remove pegana" in out
    assert "cloudflared tunnel --url" in out


# --- bundled example entry: colosseum-mcp --stdio ----------------------------


def test_colosseum_stdio_routes_and_keeps_stdout_clean(monkeypatch, capsys) -> None:
    import gecko.mcp_server as mcp_server
    import gecko.registry.client as registry_client

    monkeypatch.setenv("COLOSSEUM_COPILOT_PAT", "test-pat-not-real")
    # Force the bundled surface (no network) and skip the live PAT probe.
    monkeypatch.setattr(
        registry_client,
        "fetch_surface",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(colosseum, "_verify_pat", lambda client: (True, "ok"))

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server,
        "serve_stdio",
        lambda client, **k: captured.update({"client": client, "kwargs": k}),
    )

    rc = colosseum.main(["--stdio"])

    assert rc == 0
    assert isinstance(captured["client"], AgentApiClient)
    assert captured["kwargs"]["server_name"] == "colosseum"
    assert captured["kwargs"]["mode"] == "live"  # examples serve live
    # stdout stays the pristine MCP channel; every human line went to stderr.
    out = capsys.readouterr()
    assert out.out == ""
