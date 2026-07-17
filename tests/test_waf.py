"""Falsifier-first tests for the WAF / robot-block middleware.

The pure classifier is proved OFFLINE (no serve extra). The wired behaviour rides
Starlette's TestClient on the real ``build_multi_surface_app`` — which runs the composed
lifespan, so legit MCP traffic genuinely traverses the middleware into the mounts. Light
fakes only: the events sink override (one) + the test client.
"""

from __future__ import annotations

import logging

import pytest

from gecko.waf import WAF_ATTACK_SIGNAL, classify_path

# --------------------------------------------------------------------------- #
# The pure classifier — attack / discovery / robots / security / pass.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/.env.local",
        "/config/.env",
        "/.git/config",
        "/.git/HEAD",
        "/.aws/credentials",
        "/.ssh/id_rsa",
        "/wp-login.php",
        "/wp-admin/",
        "/wp-content/uploads/x",
        "/index.php",
        "/blog/xmlrpc.php",
        "/phpmyadmin/index.php",
        "/../../etc/passwd",
        "/a/../../secret",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/admin",
        "/config",
        "/backup.sql",
        "/db.bak",
        "/cgi-bin/test.cgi",
    ],
)
def test_attack_paths_classify_as_attack(path: str) -> None:
    assert classify_path(path) == "attack"


@pytest.mark.parametrize(
    "path",
    [
        # the GCP crawler's exact agent-discovery sweep (from the real logs)
        "/.well-known/ai-plugin.json",
        "/.well-known/mcp.json",
        "/.well-known/did.json",
        "/.well-known/agents.json",
        "/.well-known/a2a.json",
        "/.well-known/agent.json",
        "/.well-known/agent-card.json",
        "/.well-known/ai-agent.json",
        "/openrpc.json",
        "/agent-card.json",
        "/mcp/agent-card.json",
        "/agents/agent-card.json",
        "/v2/agent-card.json",
        "/v1/agent.json",
        "/v1/agent-card.json",
        "/a2a/message",
        "/a2a",
        "/agent",
    ],
)
def test_discovery_paths_classify_as_discovery(path: str) -> None:
    assert classify_path(path) == "discovery"


def test_robots_and_security_lanes() -> None:
    assert classify_path("/robots.txt") == "robots"
    assert classify_path("/.well-known/security.txt") == "security"
    assert classify_path("/security.txt") == "security"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/healthz",
        "/mcp",  # bare -> a 307 redirect, NOT a discovery probe
        "/gecko/mcp",
        "/pegana/mcp",
        "/paysh/mcp",
        "/jito/mcp",
        # the load-bearing falsifiers: our OWN manifests must NOT be swallowed
        "/.well-known/gecko.json",
        "/.well-known/x402.json",
        "/.well-known/x402",
        "/.well-known/onboard.md",
        "/comprehend",
        "/events/onboard",
        "/pegana/llms.txt",
        "/jito/tools.md",
        "/gecko/SKILL.md",  # mixed-case basename must not trip a rule
        "/pegana/.well-known/gecko.json",
        "/registry/surfaces/pegana",
    ],
)
def test_legit_paths_pass_through(path: str) -> None:
    assert classify_path(path) == "pass"


def test_case_insensitive_matching() -> None:
    # a scanner uppercasing the path must not dodge the block
    assert classify_path("/.ENV") == "attack"
    assert classify_path("/WP-Login.PHP") == "attack"
    assert classify_path("/.well-known/MCP.json") == "discovery"


# --------------------------------------------------------------------------- #
# Wired behaviour — the real multi-surface app through Starlette's TestClient.
# --------------------------------------------------------------------------- #

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import build_multi_surface_app  # noqa: E402

PEGANA = "tests/fixtures/pegana_openapi.json"
PUBLIC = "https://mcp.geckovision.tech"

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _app() -> object:
    return build_multi_surface_app(
        [("pegana", PEGANA)],
        public_url=PUBLIC,
        allowed_hosts=["testserver"],  # TestClient sends Host: testserver
    )


# --- attack lane: 403, quiet -------------------------------------------------


def test_attack_probe_gets_403() -> None:
    with TestClient(_app()) as c:
        assert c.get("/.env").status_code == 403
        assert c.get("/wp-login.php").status_code == 403
        assert c.get("/.git/config").status_code == 403


def test_attack_block_is_quiet_debug_only(caplog: pytest.LogCaptureFixture) -> None:
    # The whole point is to NOT spam the INFO access log: the WAF's own logging is DEBUG.
    with caplog.at_level(logging.DEBUG, logger="gecko.waf"):
        with TestClient(_app()) as c:
            assert c.get("/.env").status_code == 403
    waf_records = [r for r in caplog.records if r.name == "gecko.waf"]
    assert waf_records  # there IS a breadcrumb...
    assert all(
        r.levelno <= logging.DEBUG for r in waf_records
    )  # ...at DEBUG, never INFO+


# --- pass lane: every legit path is byte-identical to before -----------------


def test_legit_healthz_passes_through() -> None:
    with TestClient(_app()) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.text == "ok"


def test_real_discovery_doc_still_served() -> None:
    # THE falsifier: our own /.well-known/gecko.json must NOT be swallowed by the discovery
    # lane — routing still serves the real host manifest.
    with TestClient(_app()) as c:
        r = c.get("/.well-known/gecko.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert {s["name"] for s in r.json()["surfaces"]} == {"pegana"}


def test_bare_mcp_still_redirects_not_blocked() -> None:
    with TestClient(_app()) as c:
        r = c.get("/mcp", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/gecko/mcp"


def test_legit_mcp_initialize_passes_through() -> None:
    # A real MCP handshake at a surface mount must reach the transport untouched (200),
    # proving the streaming /{name}/mcp path is unaffected by the middleware.
    with TestClient(_app()) as c:
        r = c.post("/pegana/mcp", json=_INIT, headers=_MCP_HEADERS)
        assert r.status_code == 200


# --- hygiene lane: robots.txt + security.txt served --------------------------


def test_robots_txt_served_and_steers_off_the_noise() -> None:
    with TestClient(_app()) as c:
        r = c.get("/robots.txt")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "Disallow: /.well-known/mcp.json" in r.text
        assert "Disallow: /.well-known/oauth-authorization-server" in r.text
        # ...but it POINTS crawlers at the real discovery doc, not away from it
        assert "Allow: /.well-known/gecko.json" in r.text


def test_security_txt_served_rfc9116() -> None:
    with TestClient(_app()) as c:
        r = c.get("/.well-known/security.txt")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text.startswith("Contact:")
        assert "Expires:" in r.text  # RFC 9116 requires a future Expires
        assert f"Canonical: {PUBLIC}/.well-known/security.txt" in r.text


# --- discovery lane: soft 404 + Link breadcrumb, never a hard block ----------


def test_discovery_probe_gets_404_with_link_breadcrumb() -> None:
    with TestClient(_app()) as c:
        for path in ("/.well-known/mcp.json", "/agent-card.json", "/a2a/message"):
            r = c.get(path)
            assert r.status_code == 404, path
            link = r.headers.get("link", "")
            assert f"{PUBLIC}/mcp" in link  # points at the real MCP front door
            assert f"{PUBLIC}/.well-known/gecko.json" in link  # + the discovery doc
            body = r.json()  # a breadcrumb, not an error page
            assert body["mcp"] == f"{PUBLIC}/mcp"
            assert body["discovery"] == f"{PUBLIC}/.well-known/gecko.json"


# --- telemetry composition: attack -> one surf.blocked (robot), discovery -> none ---


def _sink() -> list[dict[str, object]]:
    from gecko import events

    docs: list[dict[str, object]] = []
    events.set_surf_sink_override(lambda d: docs.append(dict(d)))
    return docs


def test_attack_emits_one_surf_blocked_robot(monkeypatch: pytest.MonkeyPatch) -> None:
    from gecko import events

    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")  # arm the sink path
    docs = _sink()
    try:
        with TestClient(_app()) as c:
            r = c.get("/.env", headers={"user-agent": "python-requests/2.31"})
    finally:
        events.set_surf_sink_override(None)

    assert r.status_code == 403
    blocked = [d for d in docs if d["event"] == "surf.blocked"]
    assert len(blocked) == 1
    b = blocked[0]
    assert b["decision"] == "block"
    assert (
        b["client_kind"] == "robot"
    )  # composes with the uaclass ClientKind vocabulary
    assert b["reasons"] == [WAF_ATTACK_SIGNAL]
    assert b["user_agent"] == "python-requests/2.31"  # sanitized passthrough
    assert b["surface_id"] == "mcp.geckovision.tech"  # public_url folded to bare host
    assert set(b) <= events.RECORD_ALLOWED_KEYS  # control-plane: only allowlisted keys


def test_discovery_probe_emits_no_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery is an agent finding us, not an attack — it must NOT count as a block.
    from gecko import events

    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    docs = _sink()
    try:
        with TestClient(_app()) as c:
            assert c.get("/.well-known/mcp.json").status_code == 404
    finally:
        events.set_surf_sink_override(None)
    assert not any(d["event"] == "surf.blocked" for d in docs)


def test_middleware_works_without_public_url() -> None:
    # No public_url (a local serve) must not crash the middleware: relative breadcrumb,
    # no Canonical, all lanes still function.
    app = build_multi_surface_app([("pegana", PEGANA)], allowed_hosts=["testserver"])
    with TestClient(app) as c:
        assert c.get("/.env").status_code == 403
        disc = c.get("/.well-known/mcp.json")
        assert disc.status_code == 404
        assert disc.headers.get("link", "").startswith("</mcp>")  # relative breadcrumb
        assert c.get("/robots.txt").status_code == 200
        sec = c.get("/.well-known/security.txt")
        assert sec.status_code == 200
        assert "Canonical:" not in sec.text  # omitted when the host is unknown
