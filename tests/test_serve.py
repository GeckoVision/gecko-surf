"""serve.py CLI — pure helpers + main() flow (serving is monkeypatched, no socket)."""

from pathlib import Path

from gecko import serve

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")


def test_slugify():
    assert serve._slugify("Pegana API") == "pegana-api"
    assert serve._slugify("!!!") == "gecko"  # fallback


def test_mcp_url_local_vs_public():
    assert serve._mcp_url("127.0.0.1", 9000, None) == "http://127.0.0.1:9000/mcp"
    assert serve._mcp_url("0.0.0.0", 9000, "https://x.trycloudflare.com") == (
        "https://x.trycloudflare.com/mcp"
    )
    # an already-/mcp public URL isn't doubled
    assert serve._mcp_url("0.0.0.0", 9000, "https://x.dev/mcp") == "https://x.dev/mcp"


def test_main_rejects_unsafe_url(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(serve, "serve_http", lambda *a, **k: called.append(1))
    rc = serve.main(["http://169.254.169.254/openapi.json"])
    assert rc == 2
    assert not called  # never reached the server
    assert "unsafe" in capsys.readouterr().err.lower()


def test_main_prints_add_strings_and_serves(monkeypatch, capsys):
    captured = {}

    def fake_serve(client, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr(serve, "serve_http", fake_serve)
    rc = serve.main([PEGANA, "--port", "9123", "--name", "pegana"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:9123/mcp" in out
    assert "claude mcp add --transport http pegana http://127.0.0.1:9123/mcp" in out
    assert "cursor://anysphere.cursor-deeplink/mcp/install" in out
    assert "usable as tools" in out
    assert captured["kwargs"]["server_name"] == "pegana"


def test_main_public_url_added_to_allowlist(monkeypatch):
    captured = {}
    monkeypatch.setattr(serve, "serve_http", lambda client, **k: captured.update(k))
    serve.main([PEGANA, "--public-url", "https://demo.trycloudflare.com"])
    assert "demo.trycloudflare.com" in captured["allowed_hosts"]
    assert "https://demo.trycloudflare.com" in captured["allowed_origins"]
