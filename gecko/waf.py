"""WAF / robot-block middleware for the hosted multi-surface MCP server.

The public host (``mcp.geckovision.tech``) is flooded by two kinds of noise: attack
scanners probing for secrets/backups/shells, and agent-discovery crawlers machine-gunning
manifest paths we never serve. Both spray 404s that bury the real connect funnel and hand
a scanner a free map of what 404s vs 200s. This is a thin ASGI middleware that sits in
FRONT of the surface mounts and triages every request into one of five lanes BEFORE it
reaches routing:

1. **attack** — clear malicious probes (``/.env``, ``/.git``, ``/wp-*``, ``*.php``,
   path-traversal, admin/config, speculative ``/.well-known/oauth-*`` we don't serve).
   -> **403 fast**, minimal work, DEBUG-logged (never INFO), and one countable
   ``surf.blocked`` event (``client_kind=robot``) so the flood is VISIBLE, not buried.
2. **discovery** — agents probing for a manifest (``/.well-known/mcp.json``,
   ``/agent-card.json``, ``ai-plugin.json``, A2A cards). These are agents trying to FIND
   us (our ICP). We do NOT slam the door: a clean, minimal **404 carrying a ``Link:``
   header breadcrumb** to the real MCP endpoint + discovery doc. A full agent-card is a
   separate product decision — this just points the way.
3. **robots** — ``/robots.txt``, served (disallow crawlers from the noise paths).
4. **security** — ``/.well-known/security.txt`` (RFC 9116), served (a security contact —
   hygiene, and it's a probed path).
5. **pass** — everything else, forwarded UNTOUCHED to normal Starlette routing, so the
   ``/{name}/mcp`` mounts (streaming SSE), ``/healthz``, real sessions, and the real
   ``.well-known`` manifests are byte-identical to before.

**Why a pure ASGI middleware, not ``BaseHTTPMiddleware``:** the ``/{name}/mcp`` transport
streams Server-Sent Events; ``BaseHTTPMiddleware`` buffers the response and breaks
streaming. A pure ASGI wrapper forwards the original ``receive``/``send`` on the pass lane,
so the MCP transport and its DNS-rebinding ``Host`` guard (which lives INSIDE each mount)
are unaffected.

**Composition (no duplication):** the block event rides the EXISTING ``surf.blocked``
event kind and the ``ClientKind`` vocabulary that ``uaclass.classify_client`` populates for
the connect funnel — a blocked probe is a ``robot``, the same label a ``python-requests``
scanner UA earns there. The UA is sanitized by ``emit_surf_event`` (``_safe_user_agent``),
the same treatment the connect funnel gives it.

**Control plane (invariant #1):** the middleware reads only the request LINE (path/method)
+ the ``User-Agent`` — request metadata, already handled like the connect funnel. It never
reads a body, never persists a payload, and neutralizes the path/UA before logging.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Literal

from .events import emit_surf_event
from .telemetry import TelemetryError
from .uaclass import classify_client

logger = logging.getLogger("gecko.waf")

# --------------------------------------------------------------------------- #
# The probe-pattern set — the maintainable module constant. Grounded in the real
# hosted access logs (the GCP crawler's agent-discovery sweep) + the classic scanner
# targets. Every entry is a path we serve NONE of, so a match is never legitimate.
# --------------------------------------------------------------------------- #

# ATTACK — substrings that, anywhere in the (lowercased) path, mark a scanner hunting
# for a secret file, an exposed VCS/IDE dir, cloud creds, or a CMS install.
_ATTACK_SUBSTRINGS: tuple[str, ...] = (
    "/.env",  # env-file secret harvest (.env, .env.local, .env.production)
    "/.git",  # exposed git dir (/.git/config, /.git/HEAD)
    "/.svn",
    "/.hg",
    "/.aws",  # cloud credential files
    "/.ssh",
    "/.htaccess",
    "/.htpasswd",
    "/.ds_store",
    "/.vscode",
    "/.idea",
    "/wp-",  # WordPress: /wp-admin, /wp-login.php, /wp-content, /wp-json
    "/xmlrpc",
    "/phpmyadmin",
    "/cgi-bin",
)

# ATTACK — exact paths (lowercased, trailing slash stripped) that are probes on their own.
_ATTACK_EXACT: frozenset[str] = frozenset({"/admin", "/config", "/shell"})

# ATTACK — file extensions we never serve; a request ending in one is a probe.
_ATTACK_SUFFIXES: tuple[str, ...] = (
    ".php",
    ".php7",
    ".phtml",
    ".asp",
    ".aspx",
    ".jsp",
    ".cgi",
    ".sql",
    ".bak",
    ".old",
    ".backup",
    ".env",
)

# ATTACK — speculative OAuth discovery we do NOT serve (our surfaces are public / static-
# key, never an OAuth flow). Per the WAF brief this is a hard 403, not a soft breadcrumb.
_ATTACK_PREFIXES: tuple[str, ...] = ("/.well-known/oauth-",)

# DISCOVERY — basenames (the LAST path segment, lowercased) of agent/MCP discovery
# manifests. A basename match catches every observed variant in one rule:
# /.well-known/mcp.json, /agent-card.json, /mcp/agent-card.json, /agents/agent-card.json,
# /v2/agent-card.json, /v1/agent.json, /v1/agent-card.json, ... — without an exhaustive
# path list that would drift. None collide with a served basename (gecko.json / x402.json
# / llms.txt / tools.md / SKILL.md / onboard.md).
_DISCOVERY_BASENAMES: frozenset[str] = frozenset(
    {
        "agent-card.json",
        "agent.json",
        "agents.json",
        "ai-agent.json",
        "ai-plugin.json",
        "openrpc.json",
        "did.json",
        "mcp.json",
        "a2a.json",
    }
)

# DISCOVERY — bare index probes an agent tries (an /agent card root; the A2A path space).
_DISCOVERY_EXACT: frozenset[str] = frozenset({"/agent", "/agents", "/agent-card"})

# HYGIENE — the two files we SERVE (never block): robots + the RFC 9116 security contact.
_ROBOTS_PATH = "/robots.txt"
_SECURITY_PATHS: frozenset[str] = frozenset(
    {"/.well-known/security.txt", "/security.txt"}
)

# --------------------------------------------------------------------------- #
# Control-plane-safe telemetry labels (code constants — never an arg/path value).
# --------------------------------------------------------------------------- #
#: The ``reasons`` signal for a WAF block — a code constant, ``namespace.name`` shaped
#: like every risk signal (``honeypot.decoy_called``, ``gate.unscored_write``), so it
#: passes the ``events`` label validator unchanged.
WAF_ATTACK_SIGNAL = "waf.attack_probe"
#: The ``surf.blocked`` decision LABEL — reuses the existing shape-validated "block"
#: verdict; no closed-set change needed.
WAF_BLOCK_DECISION = "block"

#: A blocked probe is a robot by BEHAVIOUR (no human requests ``/.env``), so the emitted
#: ``client_kind`` is floored to ``robot`` even if the UA spoofs a real client name — the
#: request path, not the UA, is the signal here. Same ``ClientKind`` vocabulary as uaclass.
_BLOCKED_CLIENT_KIND = "robot"

# The security-contact address served in security.txt. A hygiene address (not a secret);
# the founder confirms/routes the mailbox on redeploy.
SECURITY_CONTACT = "mailto:security@geckovision.tech"

WafLane = Literal["attack", "discovery", "robots", "security", "pass"]


def classify_path(path: str) -> WafLane:
    """Triage a request path into a WAF lane. Pure + side-effect-free (testable offline).

    Order matters: traversal/null-byte first (most dangerous), then the served hygiene
    files (never mis-block them), then attack rules, then discovery, else pass. Matching
    is done on a lowercased, trailing-slash-stripped copy; the caller routes on the
    ORIGINAL path, so lowercasing here can never corrupt a real request.
    """
    # Traversal / null-byte on the RAW decoded path — a legit path never contains "..".
    if ".." in path or "\x00" in path:
        return "attack"

    p = path.lower().rstrip("/") or "/"

    # Served hygiene files — checked before the block rules so they can never be caught.
    if p == _ROBOTS_PATH:
        return "robots"
    if p in _SECURITY_PATHS:
        return "security"

    # Attack: prefixes, dotfile/CMS substrings, extensions, exact probes.
    if any(p.startswith(pre) for pre in _ATTACK_PREFIXES):
        return "attack"
    if any(sub in p for sub in _ATTACK_SUBSTRINGS):
        return "attack"
    if any(p.endswith(suf) for suf in _ATTACK_SUFFIXES):
        return "attack"
    if p in _ATTACK_EXACT:
        return "attack"

    # Discovery: a manifest basename anywhere, a bare agent index, or the A2A path space.
    basename = p.rsplit("/", 1)[-1]
    if basename in _DISCOVERY_BASENAMES:
        return "discovery"
    if p in _DISCOVERY_EXACT or p == "/a2a" or p.startswith("/a2a/"):
        return "discovery"

    return "pass"


def _user_agent(scope: Any) -> str | None:
    """Pull the raw HTTP ``User-Agent`` from an ASGI ``scope`` header list. UNTRUSTED — it
    is sanitized + capped by ``emit_surf_event`` before it is ever stored. A malformed or
    absent header is simply ``None``."""
    for key, value in scope.get("headers") or []:
        if bytes(key).lower() == b"user-agent":
            try:
                return bytes(value).decode("latin-1")
            except Exception:  # noqa: BLE001 - a malformed header is simply no UA
                return None
    return None


def _safe_for_log(text: str) -> str:
    """Neutralize a request path/UA before it reaches a log line: strip control /
    non-printable chars (log-injection carrier) and cap. Never a secret sink — a path is
    request metadata, but a hostile one must not forge a log line."""
    return "".join(ch for ch in text if ch.isprintable())[:200]


def _expires_in_a_year() -> str:
    """RFC 9116 ``Expires`` — a future UTC instant. Computed at app-build (server-boot)
    time; a ~1-year horizon comfortably outlives a redeploy cycle, so it never goes stale
    while the process runs."""
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _robots_body() -> str:
    """robots.txt: steer polite crawlers away from the noise manifests while POINTING them
    at the one discovery doc we DO serve (``/.well-known/gecko.json``). Attack scanners
    ignore robots.txt — this only quiets the well-behaved agent-discovery crawlers."""
    disallows = "\n".join(
        f"Disallow: {line}"
        for line in (
            "/agent",
            "/agents",
            "/a2a",
            "/openrpc.json",
            "/.well-known/ai-plugin.json",
            "/.well-known/mcp.json",
            "/.well-known/did.json",
            "/.well-known/agents.json",
            "/.well-known/agent.json",
            "/.well-known/agent-card.json",
            "/.well-known/ai-agent.json",
            "/.well-known/a2a.json",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
        )
    )
    return (
        "# This host serves MCP surfaces to agents.\n"
        "# Real discovery lives at /.well-known/gecko.json — please use that.\n"
        "User-agent: *\n"
        f"{disallows}\n"
        "Allow: /.well-known/gecko.json\n"
    )


def _security_body(public_url: str | None) -> str:
    """RFC 9116 security.txt — a security contact + a required future Expires. Canonical is
    added only when the host is known (``public_url``)."""
    lines = [
        f"Contact: {SECURITY_CONTACT}",
        f"Expires: {_expires_in_a_year()}",
        "Preferred-Languages: en",
    ]
    if public_url:
        lines.append(f"Canonical: {public_url.rstrip('/')}/.well-known/security.txt")
    return "\n".join(lines) + "\n"


def _breadcrumb_links(public_url: str | None) -> str:
    """The ``Link:`` header value for a discovery 404 — points a probing agent at the real
    MCP front door AND the host discovery doc. Relative when the host is unknown."""
    base = public_url.rstrip("/") if public_url else ""
    mcp = f"{base}/mcp"
    discovery = f"{base}/.well-known/gecko.json"
    return (
        f'<{mcp}>; rel="alternate"; type="application/json", '
        f'<{discovery}>; rel="service-desc"; type="application/json"'
    )


class WafMiddleware:
    """Pure-ASGI WAF: triage every HTTP request in front of the surface mounts.

    Static per host — the block/breadcrumb/robots/security responses are built once in
    ``__init__`` (the serve path, where the ``serve`` extra is present). The pass lane
    forwards the ORIGINAL ``scope``/``receive``/``send`` untouched, so SSE streaming and
    the per-mount DNS-rebinding guard are unaffected.
    """

    def __init__(self, app: Any, *, public_url: str | None = None) -> None:
        # Starlette Responses are ASGI apps (``await response(scope, receive, send)``);
        # import here so the pure classifier stays importable without the serve extra.
        from starlette.responses import PlainTextResponse, Response

        self._app = app
        # surface_id rides the events module's URL->host reduction (a full URL folds to its
        # bare host), so a host or a full public_url both land as "mcp.geckovision.tech".
        self._surface_id = public_url

        self._forbidden = PlainTextResponse(
            "forbidden\n", status_code=403, media_type="text/plain; charset=utf-8"
        )
        # A clean, minimal 404 breadcrumb — a Link header + a tiny JSON pointer, never an
        # error page. Built once (static per host).
        base = public_url.rstrip("/") if public_url else ""
        body = (
            '{"error":"not_found",'
            f'"mcp":"{base}/mcp",'
            f'"discovery":"{base}/.well-known/gecko.json",'
            '"hint":"this host serves MCP surfaces; see /.well-known/gecko.json"}\n'
        )
        self._breadcrumb = Response(
            body,
            status_code=404,
            media_type="application/json",
            headers={"Link": _breadcrumb_links(public_url)},
        )
        self._robots = PlainTextResponse(
            _robots_body(), media_type="text/plain; charset=utf-8"
        )
        self._security = PlainTextResponse(
            _security_body(public_url), media_type="text/plain; charset=utf-8"
        )

    def _emit_blocked(self, user_agent: str | None) -> None:
        """Fire the countable ``surf.blocked`` event for an attack probe. Best-effort: a
        control-plane violation (a wiring mistake) surfaces as ``TelemetryError``; any
        operational failure is swallowed so a block is never coupled to the sink."""
        try:
            emit_surf_event(
                "surf.blocked",
                surface_id=self._surface_id,
                decision=WAF_BLOCK_DECISION,
                reasons=[WAF_ATTACK_SIGNAL],
                client_kind=_BLOCKED_CLIENT_KIND,
                user_agent=user_agent,  # sanitized + capped inside emit_surf_event
            )
        except TelemetryError:
            raise
        except Exception:  # noqa: BLE001 - telemetry must never break the block response
            logger.debug("waf: surf.blocked emit failed (redacted)")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)  # lifespan / websocket pass straight
            return

        path = scope.get("path", "") or ""
        lane = classify_path(path)

        if lane == "pass":
            await self._app(scope, receive, send)
            return

        if lane == "attack":
            user_agent = _user_agent(scope)
            # DEBUG only (never INFO): the whole point is to NOT spam the access log. The
            # UA is classified for observability (a scanner spoofing a client UA still shows
            # here) even though the emitted client_kind is floored to robot.
            logger.debug(
                "waf: blocked attack path=%s ua_kind=%s",
                _safe_for_log(path),
                classify_client(user_agent, None),
            )
            self._emit_blocked(user_agent)
            await self._forbidden(scope, receive, send)
            return

        if lane == "discovery":
            # Not a block — an agent trying to discover us. Point, don't slam.
            logger.debug(
                "waf: discovery probe path=%s -> 404 breadcrumb", _safe_for_log(path)
            )
            await self._breadcrumb(scope, receive, send)
            return

        if lane == "robots":
            await self._robots(scope, receive, send)
            return

        # lane == "security"
        await self._security(scope, receive, send)


__all__ = [
    "SECURITY_CONTACT",
    "WAF_ATTACK_SIGNAL",
    "WAF_BLOCK_DECISION",
    "WafLane",
    "WafMiddleware",
    "classify_path",
]
