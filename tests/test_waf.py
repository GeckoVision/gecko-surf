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


# --------------------------------------------------------------------------- #
# 🔴 THE DANGEROUS FAILURE CLASS: a WAF that 403s a REAL agent.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        # RFC 9728 protected-resource metadata + its path-based fallback chain, and
        # RFC 8414 authorization-server metadata — the exact URLs the official MCP
        # SDK builds in mcp/client/auth/utils.py (build_protected_resource_urls /
        # build_authorization_server_urls).
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource/gecko/mcp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
    ],
)
def test_oauth_discovery_is_404_not_403(path: str) -> None:
    """A spec-compliant MCP client probes these during a normal connect.

    ``mcp/client/auth/oauth2.py::_handle_protected_resource_response`` treats **404** as
    "not supported, try the next URL" and **anything else** as
    ``raise OAuthFlowError(...)`` — so a 403 here does not merely annoy a real client,
    it ABORTS the connection. These paths must land in the soft discovery lane.
    """
    assert classify_path(path) == "discovery"
    with TestClient(_app()) as c:
        assert c.get(path).status_code == 404, path


def test_oauth_discovery_probe_is_not_counted_as_an_attack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real client doing its auth handshake must not be logged as a blocked robot.
    from gecko import events

    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    docs = _sink()
    try:
        with TestClient(_app()) as c:
            r = c.get("/.well-known/oauth-protected-resource")
    finally:
        events.set_surf_sink_override(None)
    assert r.status_code == 404
    assert not any(d["event"] == "surf.blocked" for d in docs)


# --- 🔴 the hosted surfaces must be completely unaffected --------------------

HOSTED = ("gecko", "jito", "paysh", "txline")


def _hosted_app() -> object:
    # The four surfaces the public host actually serves. The spec behind each name is
    # irrelevant to the WAF (it triages on PATH SHAPE only), so one fixture under four
    # names is the honest falsifier for "the hosted mounts still serve".
    return build_multi_surface_app(
        [(name, PEGANA) for name in HOSTED],
        public_url=PUBLIC,
        allowed_hosts=["testserver"],
    )


@pytest.mark.parametrize("name", HOSTED)
def test_hosted_surface_mcp_endpoints_still_serve(name: str) -> None:
    """/gecko/mcp, /jito/mcp, /paysh/mcp, /txline/mcp must reach the MCP transport.

    Proven by a real ``initialize`` round-trip through the middleware, not by inspection:
    a 403/404 from the WAF, or a buffered (non-streaming) response, fails this.
    """
    assert classify_path(f"/{name}/mcp") == "pass"
    with TestClient(_hosted_app()) as c:
        r = c.post(f"/{name}/mcp", json=_INIT, headers=_MCP_HEADERS)
        assert r.status_code == 200, (name, r.status_code, r.text[:200])
        assert "mcp-session-id" in {k.lower() for k in r.headers}
        assert "serverInfo" in r.text


@pytest.mark.parametrize("name", HOSTED)
def test_hosted_surface_healthz_and_artifacts_still_serve(name: str) -> None:
    with TestClient(_hosted_app()) as c:
        assert c.get(f"/{name}/healthz").status_code == 200
        assert c.get(f"/{name}/.well-known/gecko.json").status_code == 200


def test_host_healthz_and_root_still_serve() -> None:
    with TestClient(_hosted_app()) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 200
        gecko_json = c.get("/.well-known/gecko.json")
        assert gecko_json.status_code == 200
        assert {s["name"] for s in gecko_json.json()["surfaces"]} == set(HOSTED)


# --- bypass belt: normalization must not be dodgeable -----------------------


@pytest.mark.parametrize(
    "path",
    [
        "//admin",  # duplicate leading slash dodges an exact-set match
        "///admin",
        "/admin/",  # trailing slash
        "//config",
        "/.git//config",
        "//.env",
        "/a//..//b",  # traversal behind a doubled slash
    ],
)
def test_duplicate_slash_does_not_bypass_the_attack_lane(path: str) -> None:
    assert classify_path(path) == "attack"


@pytest.mark.parametrize("path", ["//agent", "//a2a/message", "//agent-card.json"])
def test_duplicate_slash_does_not_bypass_the_discovery_lane(path: str) -> None:
    assert classify_path(path) == "discovery"


def test_slash_normalization_does_not_break_legit_paths() -> None:
    # The normalizer must never turn a real surface path into a blocked one.
    for path in ("//gecko/mcp", "/gecko//mcp", "/gecko/mcp/", "//healthz"):
        assert classify_path(path) == "pass", path


def test_blocked_event_carries_no_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control plane: the probe PATH is request content — it must never leave the process.

    Complements the RECORD_ALLOWED_KEYS bound with a direct value-level check.
    """
    from gecko import events

    monkeypatch.setenv("MONGODB_URI", "mongodb://fake")
    docs = _sink()
    secret_ish = "/.env.production.deadbeefsecret"
    try:
        with TestClient(_app()) as c:
            assert (
                c.get(
                    secret_ish, headers={"authorization": "Bearer SUPERSECRET"}
                ).status_code
                == 403
            )
    finally:
        events.set_surf_sink_override(None)

    blocked = [d for d in docs if d["event"] == "surf.blocked"]
    assert len(blocked) == 1
    serialized = repr(blocked[0])
    assert "deadbeefsecret" not in serialized  # no path
    assert "SUPERSECRET" not in serialized  # no headers/credentials
    assert "authorization" not in serialized.lower()


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
