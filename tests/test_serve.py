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


def test_main_base_url_pins_the_trust_anchor(monkeypatch):
    monkeypatch.setattr(serve, "serve_http", lambda client, **k: None)
    # This test is about CLI wiring (base_url -> AgentApiClient -> anchor), not SSRF
    # resolution — netguard's own tests cover validate_public_url. No real DNS here.
    monkeypatch.setattr(serve, "validate_public_url", lambda *a, **k: None)
    captured = {}
    real_client = serve.AgentApiClient

    def spy(*a, **k):
        client = real_client(*a, **k)
        captured["client"] = client
        return client

    monkeypatch.setattr(serve, "AgentApiClient", spy)
    rc = serve.main([PEGANA, "--base-url", "https://api.example.com"])
    assert rc == 0
    assert captured["client"].anchor.state == "pinned"
    assert "api.example.com" in captured["client"]._auth_allowed_hosts


def test_main_rejects_unsafe_base_url(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(serve, "serve_http", lambda *a, **k: called.append(1))
    rc = serve.main([PEGANA, "--base-url", "http://169.254.169.254/"])
    assert rc == 2
    assert not called
    assert "unsafe" in capsys.readouterr().err.lower()


def test_stdio_spawn_includes_base_url_near_auth_keychain():
    args = serve._parse_args(
        [PEGANA, "--base-url", "https://api.example.com", "--stdio"]
    )
    spawn = serve._stdio_spawn(args)
    assert "--base-url https://api.example.com" in spawn


def test_stdio_spawn_omits_base_url_when_absent():
    args = serve._parse_args([PEGANA, "--stdio"])
    assert "--base-url" not in serve._stdio_spawn(args)
