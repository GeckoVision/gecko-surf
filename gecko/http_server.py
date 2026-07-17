"""HTTP transport — serve the EXISTING ``McpSurface`` over MCP Streamable HTTP.

This is the thin distribution edge for M1: one public OpenAPI URL, comprehended by
the unchanged engine, exposed at a single ``/mcp`` endpoint a real external agent
(Claude Code / Cursor) can add. The comprehension layer is reused verbatim — this
module only bridges ``McpSurface`` to the wire.

Design notes:
- The ``mcp`` SDK + ``starlette`` + ``uvicorn`` live behind the optional ``serve``
  extra, so the import is guarded (mirrors ``mcp_server.serve_stdio``). The engine
  stays dep-light.
- We register tools on the *low-level* MCP ``Server`` rather than ``FastMCP`` so the
  question-shaped ``inputSchema`` reaches the agent intact (first-call-correct);
  FastMCP infers schemas from a Python signature, which would erase ours.
- DNS-rebinding defense is on: the transport validates the ``Host``/``Origin``
  headers against an explicit allowlist.
- Control plane: a call's response flows back in the JSON-RPC reply but is NEVER
  persisted or logged. We log only redacted correctness metadata (tool, status, ok).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import corpus
from .access import public_session
from .caller import CallError
from .agentnative import build_artifacts
from .client import AgentApiClient
from .modes import CallMode
from .enforce import EnforceMode, resolve_hosted_enforce
from .events import _safe_user_agent, emit_surf_event
from .mcp_server import McpSurface
from .telemetry import TelemetryError
from .uaclass import classify_client

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette

logger = logging.getLogger("gecko.http_server")

DEFAULT_SERVER_NAME = "gecko"
MCP_PATH = "/mcp"

# The 'submit your API' front doors: a human/agent HTTP POST and an agent MCP tool.
COMPREHEND_PATH = "/comprehend"
META_SURFACE_NAME = "gecko"  # the meta MCP surface mounts at /gecko/mcp
# A submission body is a tiny JSON envelope ({"url": ...}); cap it hard.
MAX_COMPREHEND_REQUEST_BYTES = 64 * 1024

# The `gecko add` onboard-ping ingest — the attribution event that makes adopters
# visible. Aggregate-only + control-plane: five short labels, never a payload/arg/
# secret/user-datum. Every rejection answers the SAME empty 204 as success, so a
# scraper probing the path learns nothing.
EVENTS_ONBOARD_PATH = "/events/onboard"
# A ping is a tiny fixed envelope; anything bigger is not a ping.
MAX_ONBOARD_PING_BYTES = 2 * 1024
#: EXACTLY the keys a ping may carry — an unknown OR missing key rejects the body.
ONBOARD_PING_KEYS: frozenset[str] = frozenset(
    {"surface_host", "version", "client_os", "install_id", "mode"}
)
_MAX_ONBOARD_VALUE = 64
# `gecko add --mode` offers only recorded|live, so the ping set is deliberately
# NARROWER than modes.CALL_MODES ("probe" is an engine mode, never an onboard).
_ONBOARD_PING_MODES: frozenset[str] = frozenset({"recorded", "live"})


def parse_onboard_ping(body: bytes) -> dict[str, str] | None:
    """Strictly validate an onboard-ping body; ``None`` on ANY deviation (fail closed).

    A valid body is a small JSON object carrying EXACTLY ``ONBOARD_PING_KEYS``, every
    value a non-empty string of at most ``_MAX_ONBOARD_VALUE`` chars, and ``mode`` from
    the closed recorded|live set. Junk JSON, an unknown/missing key, an oversized
    body/value, a non-string — anything else yields ``None`` so the route emits nothing
    (and still 204s; the caller never differentiates rejections on the wire)."""
    if len(body) > MAX_ONBOARD_PING_BYTES:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):  # ValueError covers JSONDecodeError
        return None
    if not isinstance(payload, dict) or set(payload) != ONBOARD_PING_KEYS:
        return None
    fields: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value or len(value) > _MAX_ONBOARD_VALUE:
            return None
        fields[key] = value
    if fields["mode"] not in _ONBOARD_PING_MODES:
        return None
    return fields


# Trusted proxy range for uvicorn's X-Forwarded-For handling. Default "*" is safe here:
# the ONLY ingress is the ALB (no direct public route to the task), so no untrusted peer
# can reach us to spoof the header. Override to a CIDR to tighten it (redeploy).
FORWARDED_ALLOW_IPS_ENV = "GECKO_FORWARDED_ALLOW_IPS"

_INSTALL_HINT = (
    "Install the serve extra to run the HTTP server: uv sync --extra serve "
    "(or: uv pip install 'gecko-surf[serve]')"
)


# The MCP transport returns/reads the session id in this header (mcp SDK constant).
_MCP_SESSION_ID_HEADER = "mcp-session-id"
# We only ever peek at the first slice of a POST body to spot an `initialize` frame; an
# MCP JSON-RPC request is tiny, so this cap keeps a hostile large body out of memory
# while never truncating a real handshake.
_INIT_PARSE_CAP = 64 * 1024


async def _tee_body(receive: Any) -> tuple[bytes, list[dict[str, Any]]]:
    """Drain the ASGI request messages, returning a bounded prefix of the body (for
    handshake detection) AND the exact messages read (for byte-for-byte replay).

    This NEVER consumes the body from the transport's point of view: the messages are
    replayed verbatim by ``_make_replay``, so the streamable-http transport sees the
    unmodified request.
    """
    messages: list[dict[str, Any]] = []
    prefix = bytearray()
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") == "http.request":
            body = message.get("body", b"")
            if len(prefix) < _INIT_PARSE_CAP:
                prefix.extend(body[: _INIT_PARSE_CAP - len(prefix)])
            if not message.get("more_body", False):
                break
        else:  # http.disconnect or anything else — stop, nothing more to buffer
            break
    return bytes(prefix), messages


def _make_replay(messages: list[dict[str, Any]], receive: Any) -> Any:
    """A replacement ``receive`` that replays the buffered messages, then defers to the
    real ``receive`` for anything after (e.g. a later disconnect)."""
    index = 0

    async def _replay() -> dict[str, Any]:
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return await receive()  # type: ignore[no-any-return]

    return _replay


def _parse_initialize(prefix: bytes) -> tuple[bool, str | None]:
    """Return ``(is_initialize, raw_client)`` for a buffered request-body prefix.

    Detects a JSON-RPC ``"method":"initialize"`` frame (single or batched) and pulls
    ``params.clientInfo`` name/version into a raw ``"name/version"`` string. The raw
    string is UNTRUSTED — ``emit_surf_event`` sanitizes + caps it. Any parse failure is
    a clean "not an initialize" (best-effort; never raises)."""
    try:
        obj: Any = json.loads(prefix)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return (False, None)
    frames = obj if isinstance(obj, list) else [obj]
    for frame in frames:
        if isinstance(frame, dict) and frame.get("method") == "initialize":
            client: str | None = None
            params = frame.get("params")
            info = params.get("clientInfo") if isinstance(params, dict) else None
            if isinstance(info, dict):
                name = info.get("name")
                version = info.get("version")
                if isinstance(name, str) and name:
                    client = (
                        f"{name}/{version}"
                        if isinstance(version, str) and version
                        else name
                    )
            return (True, client)
    return (False, None)


def _session_id_from_start_headers(headers: Any) -> str | None:
    """Pull the ``mcp-session-id`` the transport assigned, from an ASGI
    ``http.response.start`` header list (list of ``(name, value)`` byte tuples)."""
    for key, value in headers or []:
        if bytes(key).lower() == b"mcp-session-id":
            try:
                return bytes(value).decode("latin-1")
            except Exception:  # noqa: BLE001 - a malformed header is simply no session id
                return None
    return None


def _user_agent_from_scope(scope: Any) -> str | None:
    """Pull the raw HTTP ``User-Agent`` from an ASGI ``scope`` header list (list of
    ``(name, value)`` byte tuples). UNTRUSTED — the caller sanitizes + caps it before it
    is ever stored (``_safe_user_agent``). A malformed/absent header is simply ``None``."""
    for key, value in scope.get("headers") or []:
        if bytes(key).lower() == b"user-agent":
            try:
                return bytes(value).decode("latin-1")
            except Exception:  # noqa: BLE001 - a malformed header is simply no UA
                return None
    return None


def _emit_connect_outcome(
    surface_id: str,
    status: int,
    *,
    client: str | None,
    session_id: str | None,
    user_agent: str | None,
    client_kind: str,
    is_init: bool,
) -> None:
    """Emit the control-plane-safe funnel event for a POST outcome to ``/mcp``.

    * An ``initialize`` that 2xx is a real ``surf.connect`` (with the assigned session id).
    * An ``initialize`` that 4xx is a failed handshake (stale-session 400/406 clients).
    * A NON-initialize POST that 4xx is a pure crawler/prober that never opened a session
      — captured as ``surf.connect_failed`` (``client`` is ``None``, so classification is
      UA-only) so bots are visible in the funnel. Non-init 2xx (a normal tools/call) and
      any other status emit NOTHING — the conditionals ARE the noise guard.

    Every emit carries the sanitized ``user_agent`` + robot/human ``client_kind``.
    Best-effort: only a control-plane violation surfaces (telemetry never breaks a call).
    """
    try:
        if is_init and 200 <= status < 300:
            emit_surf_event(
                "surf.connect",
                surface_id=surface_id,
                client=client,
                session_id=session_id,
                user_agent=user_agent,
                client_kind=client_kind,
            )
        elif 400 <= status < 500:
            emit_surf_event(
                "surf.connect_failed",
                surface_id=surface_id,
                client=client,
                user_agent=user_agent,
                client_kind=client_kind,
            )
    except TelemetryError:
        raise
    except Exception:  # noqa: BLE001 - telemetry must never break the handshake
        logger.warning("surf.connect emit failed (redacted)")


class _InitializeCaptureASGI:
    """Thin ASGI wrapper over the per-surface ``/mcp`` app that observes the MCP
    ``initialize`` handshake and emits ``surf.connect`` / ``surf.connect_failed``.

    It is a class (not a function) so Starlette routes it as an ASGI app, not a
    request/response endpoint. It NEVER mutates the request or response: the body is
    tee'd and replayed byte-for-byte, and the response is passed straight through — so
    the streamable-http transport, the DNS-rebinding ``Host`` guard (which lives inside
    the wrapped app), and ``/healthz`` (a sibling route, never wrapped) are untouched.
    """

    def __init__(self, inner: Any, surface_id: str) -> None:
        self._inner = inner
        self._surface_id = surface_id

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self._inner(scope, receive, send)
            return
        prefix, messages = await _tee_body(receive)
        is_init, client = _parse_initialize(prefix)
        replay = _make_replay(messages, receive)
        # UA is request metadata (fine to store once sanitized); no raw IP (PII) here.
        user_agent = _safe_user_agent(_user_agent_from_scope(scope))
        client_kind = classify_client(user_agent, client)

        emitted = False
        surface_id = self._surface_id

        async def _send(event: dict[str, Any]) -> None:
            nonlocal emitted
            if not emitted and event.get("type") == "http.response.start":
                emitted = True
                status = int(event.get("status", 0))
                # session id is only assigned on a successful init handshake.
                session_id = (
                    _session_id_from_start_headers(event.get("headers"))
                    if is_init
                    else None
                )
                _emit_connect_outcome(
                    surface_id,
                    status,
                    client=client,
                    session_id=session_id,
                    user_agent=user_agent,
                    client_kind=client_kind,
                    is_init=is_init,
                )
            await send(event)

        # Every POST is now tee'd through _send so a non-init crawler 4xx is observable;
        # the wrapper still NEVER mutates the request/response (pass-through send).
        await self._inner(scope, replay, _send)


def _session_id_from_context(server: Any) -> str | None:
    """Best-effort read of the MCP session id from the low-level server's per-request
    context (the transport attaches the Starlette ``Request``, whose headers carry
    ``mcp-session-id``). Returns ``None`` outside a request or when absent."""
    try:
        ctx = server.request_context
    except LookupError:
        return None
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get(_MCP_SESSION_ID_HEADER)
    except Exception:  # noqa: BLE001 - a non-mapping request object is simply no session
        return None
    return value if isinstance(value, str) else None


def _surface_from(
    spec_or_client: Any,
    base_url: str | None,
    mode: CallMode,
    enforce: EnforceMode | None = None,
) -> Any:
    """Accept a spec (str/dict), an AgentApiClient, an McpSurface, or any duck-typed
    surface (``list_tools`` + ``call_tool``); yield a surface.

    A bare spec is wrapped with a ``public_session`` so auth-gated ops stay hidden —
    M1 is public-only, and the agent must never be offered a tool it can't satisfy. The
    duck-typed branch admits the synthetic ``MetaComprehendSurface`` (one tool, no
    client) without forcing it through ``AgentApiClient``.

    ``enforce`` sets the call-time risk gate stance on any McpSurface this builds. A
    pre-built McpSurface is left as-is (it carries its own stance); the meta surface is
    duck-typed and has no gate.
    """
    if isinstance(spec_or_client, McpSurface):
        return spec_or_client
    if isinstance(spec_or_client, AgentApiClient):
        return McpSurface(spec_or_client, mode=mode, enforce=enforce)
    if hasattr(spec_or_client, "list_tools") and hasattr(spec_or_client, "call_tool"):
        return spec_or_client
    client = AgentApiClient(spec_or_client, base_url=base_url, session=public_session())
    return McpSurface(client, mode=mode, enforce=enforce)


def _log_outcome(name: str, result: Any) -> None:
    """Log ONLY redacted correctness metadata — never the payload (control plane).

    Extracts the status code (correctness signal) and an ok flag; the response body
    is deliberately untouched and unlogged.
    """
    status = result.get("status") if isinstance(result, dict) else None
    ok = status is None or (isinstance(status, int) and 200 <= status < 400)
    logger.info("call tool=%s status=%s ok=%s", name, status, ok)


def build_http_app(
    spec_or_client: Any,
    *,
    base_url: str | None = None,
    mode: CallMode = "recorded",
    server_name: str = DEFAULT_SERVER_NAME,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    corpus_path: str | Path | None = None,
    surface_id: str | None = None,
    surface_rev: str = "0",
    public_url: str | None = None,
    enforce: EnforceMode | None = None,
) -> Starlette:
    """Build the Streamable-HTTP ASGI app wrapping ``McpSurface`` (no server run).

    Factored out of ``serve_http`` so tests can mount it in-process (offline) with an
    ASGI transport. ``allowed_hosts``/``allowed_origins`` drive DNS-rebinding defense.

    ``enforce`` sets the call-time risk gate stance for the wrapped surface (block |
    warn | off); ``None`` defers to the ``McpSurface`` default (``GECKO_ENFORCE``, else
    warn). The multi-surface builder injects the hosted ``block`` default.

    ``corpus_path`` enables Phase-0 correctness-corpus capture: when set, each proxied
    operation appends one control-plane-safe metadata record (see ``gecko.corpus``).
    It is **off by default** — sitting in the data path and persisting any metadata is
    the founder-ratified decision (spec §7-#1), so the caller must opt in explicitly.
    Capture is metadata-only by construction: the writer never receives the response
    body or filled URL.
    """
    try:
        import mcp.types as mcp_types
        from mcp.server.fastmcp.server import StreamableHTTPASGIApp
        from mcp.server.lowlevel import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse, Response
        from starlette.routing import Route
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SystemExit(_INSTALL_HINT) from exc

    surface = _surface_from(spec_or_client, base_url, mode, enforce)
    tools = surface.list_tools()

    # Build the capture context once (zero request scope): the templated _invoke per
    # operation, and whether the session carries auth. Comes from the underlying
    # client's FULL tool defs — never from `surface.list_tools()`, which strips _invoke.
    invoke_by_name: dict[str, dict[str, Any]] = {}
    session_has_auth = False
    if corpus_path is not None:
        client = getattr(surface, "client", None)
        for t in getattr(client, "list_tools", list)():
            inv = t.get("_invoke")
            if isinstance(inv, dict):
                invoke_by_name[t["name"]] = inv
        session_has_auth = bool(getattr(client, "_session_has_auth", False))
    cid = surface_id or server_name

    def _capture(
        name: str,
        status: int | None,
        exc: BaseException | None,
        args: dict[str, Any],
        latency_ms: int | None,
    ) -> None:
        # search_capabilities is synthetic (no upstream call) — never a corpus record.
        invoke = invoke_by_name.get(name)
        if invoke is None:
            return
        corpus.record(
            corpus.outcome_from(
                operation_id=name,
                tool_invoke=invoke,
                args=args,
                status=status,
                error_class=corpus.error_class_for(status, exc),
                latency_ms=latency_ms,
                mode=mode,
                auth_injected=session_has_auth,
                ts=int(time.time() * 1000),
                surface_id=cid,
                surface_rev=surface_rev,
            ),
            corpus_path,  # type: ignore[arg-type]
        )

    server: Any = Server(server_name)

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [
            mcp_types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in tools
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[Any]:
        args = arguments or {}
        start = time.perf_counter()
        # Thread the MCP session id onto the usage event so the funnel can join
        # connect->call per session (retention). Only McpSurface accepts it; the meta
        # surface (duck-typed) does not, so it is passed conditionally.
        session_id = _session_id_from_context(server)
        try:
            if isinstance(surface, McpSurface):
                result = surface.call_tool(name, args, session_id=session_id)
            else:
                result = surface.call_tool(name, args)
        except CallError as exc:
            # A pre-flight failure (missing path param / auth-gated) is itself a
            # first-call outcome worth capturing; record it, then propagate as before.
            if corpus_path is not None:
                _capture(name, None, exc, args, None)
            raise
        status = result.get("status") if isinstance(result, dict) else None
        _log_outcome(name, result)
        if corpus_path is not None:
            _capture(
                name, status, None, args, int((time.perf_counter() - start) * 1000)
            )
        # Return as unstructured JSON text; never cache/persist the body.
        return [
            mcp_types.TextContent(type="text", text=json.dumps(result, default=str))
        ]

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts or [],
        allowed_origins=allowed_origins or [],
    )
    manager = StreamableHTTPSessionManager(app=server, security_settings=security)
    # Wrap the transport app with the funnel capture: it observes the `initialize`
    # handshake and emits surf.connect / surf.connect_failed WITHOUT touching the
    # request/response (tee+replay body, pass-through send), so the streamable-http
    # transport and its DNS-rebinding Host guard are unaffected.
    asgi_app = _InitializeCaptureASGI(StreamableHTTPASGIApp(manager), cid)

    async def _healthz(_request: Any) -> Any:
        # Plain Starlette route — it never enters StreamableHTTPASGIApp, so the
        # DNS-rebinding guard (which only wraps /mcp) doesn't run here. The ALB
        # target-group health check sends Host: <task-ip>:8000, which the
        # allowed_hosts allowlist would otherwise reject — bypassing it keeps the
        # target healthy without allowlisting the private IP. Matcher = 200.
        return PlainTextResponse("ok")

    # Agent-native discovery surface for THIS API — llms.txt / gecko.json /
    # .well-known/gecko.json / tools.md, generated from the comprehended surface
    # (control-plane only). Plain routes, like /healthz: public metadata, no /mcp
    # rebinding guard needed. Built once at app-build time (static per surface).
    _ARTIFACT_MEDIA = {
        "llms.txt": "text/plain; charset=utf-8",
        "gecko.json": "application/json",
        ".well-known/gecko.json": "application/json",
        "tools.md": "text/markdown; charset=utf-8",
        "SKILL.md": "text/markdown; charset=utf-8",
    }
    artifact_routes: list[Any] = []
    client_for_emit = getattr(surface, "client", None)
    if isinstance(client_for_emit, AgentApiClient):
        mcp_url = f"{public_url.rstrip('/')}{MCP_PATH}" if public_url else None
        artifacts = build_artifacts(
            client_for_emit, mcp_url=mcp_url, site_url=public_url
        )
        for rel, text in artifacts.items():

            def _artifact_endpoint(
                _request: Any, _text: str = text, _rel: str = rel
            ) -> Any:
                return Response(_text, media_type=_ARTIFACT_MEDIA[_rel])

            artifact_routes.append(Route("/" + rel, endpoint=_artifact_endpoint))

    return Starlette(
        routes=[
            Route("/healthz", endpoint=_healthz),
            *artifact_routes,
            Route(MCP_PATH, endpoint=asgi_app),
        ],
        lifespan=lambda _app: manager.run(),
    )


def build_multi_surface_app(
    surfaces: list[tuple[str, Any]],
    *,
    mode: CallMode = "recorded",
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    public_url: str | None = None,
    enforce: EnforceMode | None = None,
    registry_routes: list[Any] | None = None,
    background_tasks: list[Callable[[], Coroutine[Any, Any, None]]] | None = None,
) -> Starlette:
    """Serve MANY comprehended surfaces from one host — the centralization surface.

    ``surfaces`` is ``[(name, spec_or_client), ...]``. Each is mounted under ``/{name}``,
    so an agent adds ``/{name}/mcp`` and finds ``/{name}/llms.txt`` etc. — every API on
    one server, each with its own clean discovery surface. A root ``/healthz`` fronts the
    ALB check and ``/`` lists what's available.

    This is the HOSTED path: ``enforce`` resolves ``GECKO_ENFORCE`` with a ``block``
    default (the risk gate is ACTIVE by default here — a redeploy is required to change
    it), and is threaded into every per-surface McpSurface so each gets an auto-derived
    policy + the same stance.

    Starlette does NOT run a mounted sub-app's lifespan, but each surface's MCP session
    manager MUST be started for the whole server lifetime — so we compose every sub-app's
    lifespan explicitly via an ``AsyncExitStack`` (get this wrong and ``/{name}/mcp`` 500s).

    ``registry_routes`` optionally appends the Gecko registry HTTP surface (built via
    ``gecko.registry.api.registry_routes``) — anonymous free-surface fetch + the
    premium 402 entitlement gate + OTP key issuance, at ``/registry/...``.

    ``background_tasks`` are long-lived coroutine factories started on app startup and
    cancelled on shutdown (e.g. the pay.sh catalog self-refresh drift-watch). They run for
    the whole server lifetime, inside the same composed lifespan as the MCP mounts. The
    transport stays generic — it never imports any specific surface; a task just gets a
    coroutine factory it drives and cancels.
    """
    from contextlib import AsyncExitStack, asynccontextmanager
    from dataclasses import asdict

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import (
        JSONResponse,
        PlainTextResponse,
        RedirectResponse,
        Response,
    )
    from starlette.routing import Mount, Route

    from .comprehend_service import (
        ComprehendError,
        comprehend_submission,
        ensure_submittable,
    )
    from .mcp_server import MetaComprehendSurface
    from .waf import WafMiddleware
    from .wellknown import build_onboard_breadcrumb, build_x402_manifest

    # Hosted default resolved in ONE place (enforce.resolve_hosted_enforce): explicit wins,
    # else GECKO_ENFORCE, else block. Same call the single-surface serve_http makes.
    hosted_enforce = resolve_hosted_enforce(enforce)

    subs: list[tuple[str, Starlette]] = []
    for name, spec in surfaces:
        if registry_routes and name == "registry":
            raise ValueError(
                "surface name 'registry' is reserved (would shadow /registry/*)"
            )
        site = f"{public_url.rstrip('/')}/{name}" if public_url else None
        subs.append(
            (
                name,
                build_http_app(
                    spec,
                    mode=mode,
                    server_name=name,
                    allowed_hosts=allowed_hosts,
                    allowed_origins=allowed_origins,
                    public_url=site,
                    enforce=hosted_enforce,
                ),
            )
        )

    # The 'submit your API' meta surface — one MCP tool (comprehend_api) mounted at
    # /gecko/mcp. Its HTTP sibling is POST /comprehend below; both call the SAME core
    # (one engine, two front doors). Comprehend-and-return only: it never hosts or
    # publicly lists a submission (no public catalog — a hard invariant).
    meta_site = f"{public_url.rstrip('/')}/{META_SURFACE_NAME}" if public_url else None
    meta_sub = build_http_app(
        MetaComprehendSurface(),
        mode=mode,
        server_name=META_SURFACE_NAME,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        public_url=meta_site,
    )

    def _abs(path: str) -> str:
        return f"{public_url.rstrip('/')}{path}" if public_url else path

    index = {
        "name": "gecko",
        "description": "Comprehended API surfaces, served agent-native.",
        # Comprehended surfaces served on this host (NOT a public marketplace listing —
        # each is a spec the operator chose to serve). Submissions are never added here.
        "surfaces": [
            {
                "name": name,
                "mcp": f"{public_url.rstrip('/')}/{name}/mcp"
                if public_url
                else f"/{name}/mcp",
                "llms_txt": f"/{name}/llms.txt",
            }
            for name, _ in subs
        ],
        # The submit-your-API front doors — comprehend and return to the submitter only.
        "submit": {
            "http": _abs(COMPREHEND_PATH),
            "mcp": _abs(f"/{META_SURFACE_NAME}/mcp"),
            "tool": "comprehend_api",
            "description": (
                "POST an OpenAPI URL to comprehend it into first-call-correct tools, "
                "returned to you only. Not hosted or publicly listed."
            ),
        },
        # Onboarding for BOTH audiences, tied to the canonical docs. Flows to `/` AND
        # `.well-known/gecko.json` (single index dict), so either probe finds the door.
        "getting_started": {
            "use_an_api": {
                "description": (
                    "Add any surface to your agent and call it correctly on the first "
                    "try."
                ),
                # Literal <name> is a template placeholder the developer fills in.
                "add": "claude mcp add --transport http <name> " + _abs("/<name>/mcp"),
                "then": (
                    "call the search_capabilities tool to find the right operation, "
                    "then call it"
                ),
                "docs": "https://docs.geckovision.tech/quickstart",
            },
            "onboard_your_api": {
                "description": (
                    "Make your own API agent-usable — first-call-correct tools; if you "
                    "charge, you keep 100%."
                ),
                "self_serve": (
                    _abs(COMPREHEND_PATH)
                    + "  (or the comprehend_api tool at "
                    + _abs("/" + META_SURFACE_NAME + MCP_PATH)
                    + ")"
                ),
                "docs": "https://docs.geckovision.tech/for-providers",
            },
        },
    }

    async def _healthz(_request: Any) -> Any:
        return PlainTextResponse("ok")

    async def _index(_request: Any) -> Any:
        return JSONResponse(index)

    async def _mcp_root_redirect(_request: Any) -> Any:
        # /mcp is the conventional default path a real MCP client tries; it lives only
        # at /{name}/mcp and /gecko/mcp, so a bare POST /mcp used to 404 (silent
        # onboarding failure). 307 preserves method+body and points at the meta front
        # door. Whether a given MCP client auto-follows a 307 on POST is the live-smoke
        # check (Pattern B): httpx/fetch follow by default, but the founder confirms it.
        return RedirectResponse(url=f"/{META_SURFACE_NAME}{MCP_PATH}", status_code=307)

    async def _well_known_gecko(_request: Any) -> Any:
        # Host-level discovery — the SAME content _index returns (surfaces + submit door).
        return JSONResponse(index)

    async def _well_known_x402(_request: Any) -> Any:
        # Honest, control-plane-safe x402 stance: Gecko composes x402, custody none.
        return JSONResponse(build_x402_manifest(surfaces, public_url))

    # Built once (static per host): both onboarding paths + the canonical doc links.
    _onboard_md = build_onboard_breadcrumb(public_url)

    async def _well_known_onboard(_request: Any) -> Any:
        # A short breadcrumb pointing both audiences at the canonical docs — not a copy.
        return Response(_onboard_md, media_type="text/markdown; charset=utf-8")

    async def _comprehend(request: Request) -> Any:
        # Size cap BEFORE reading the body (Content-Length hint) and again after.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit():
            if int(declared) > MAX_COMPREHEND_REQUEST_BYTES:
                return JSONResponse(
                    {"error": "request body too large"}, status_code=413
                )
        body = await request.body()
        if len(body) > MAX_COMPREHEND_REQUEST_BYTES:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=400
            )
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            return JSONResponse({"error": "missing 'url'"}, status_code=400)
        try:
            ensure_submittable(url)  # remote door: http(s) only, no local file read
            result = comprehend_submission(
                url, from_docs=bool(payload.get("from_docs", False))
            )
        except ComprehendError as exc:
            # The message is already redacted of any URL credential (safe to return).
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:  # noqa: BLE001 - never leak a stack / 500 from this door
            logger.exception("unexpected error comprehending a submission")
            return JSONResponse(
                {"error": "could not comprehend that URL — please try again"},
                status_code=502,
            )
        return JSONResponse(asdict(result))

    async def _events_onboard(request: Request) -> Any:
        # ALWAYS an empty 204 — success and every rejection look identical on the
        # wire, so a probing scraper learns nothing. Fire-and-forget: ANY failure
        # (junk body, a value the events module refuses, a sink error) emits nothing
        # and still 204s; this route can never 500.
        try:
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit():
                if int(declared) > MAX_ONBOARD_PING_BYTES:
                    return Response(status_code=204)
            fields = parse_onboard_ping(await request.body())
            if fields is not None:
                # surface_id rides the events module's existing opaque-token
                # reduction: a URL is cut to its bare host, a secret-shaped id folds
                # to a stable hash — a credential can never be stored.
                emit_surf_event(
                    "surf.onboard",
                    surface_id=fields["surface_host"],
                    version=fields["version"],
                    client_os=fields["client_os"],
                    install_id=fields["install_id"],
                    mode=fields["mode"],
                )
        except Exception:  # noqa: BLE001 - incl. TelemetryError: hostile wire input
            # must fail closed to "emit nothing", never to a scraper-visible 500.
            logger.warning("onboard ping rejected (redacted)")
        return Response(status_code=204)

    routes: list[Any] = [
        Route("/healthz", endpoint=_healthz),
        Route("/", endpoint=_index),
        # Root /mcp alias -> the meta front door (was 404; conventional default path).
        Route(MCP_PATH, endpoint=_mcp_root_redirect, methods=["GET", "POST"]),
        # Host-level discovery the public app serves at the root (per-surface artifacts
        # live inside each mount; these are the WHOLE-HOST manifests a root probe hits).
        Route("/.well-known/gecko.json", endpoint=_well_known_gecko),
        Route("/.well-known/x402.json", endpoint=_well_known_x402),
        Route("/.well-known/x402", endpoint=_well_known_x402),
        Route("/.well-known/onboard.md", endpoint=_well_known_onboard),
        Route(COMPREHEND_PATH, endpoint=_comprehend, methods=["POST"]),
        # The `gecko add` onboard-ping ingest (see parse_onboard_ping above).
        Route(EVENTS_ONBOARD_PATH, endpoint=_events_onboard, methods=["POST"]),
    ]
    for name, sub in subs:
        routes.append(Mount(f"/{name}", app=sub))
    routes.append(Mount(f"/{META_SURFACE_NAME}", app=meta_sub))
    if registry_routes:
        routes.extend(registry_routes)

    @asynccontextmanager
    async def _lifespan(_app: Starlette) -> Any:
        async with AsyncExitStack() as stack:
            for _name, sub in subs:
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            # The meta surface's MCP session manager must start too (else /gecko/mcp 500s).
            await stack.enter_async_context(meta_sub.router.lifespan_context(meta_sub))
            # Long-lived background workers (e.g. pay.sh self-refresh) run for the whole
            # server lifetime and are cancelled cleanly on shutdown.
            tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(fn()) for fn in (background_tasks or [])
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task

    # The WAF/robot-block middleware runs BEFORE routing (in front of every surface mount),
    # so attack scanners and agent-discovery crawlers are triaged away before they reach —
    # or even see — the mounts. Pure ASGI (never BaseHTTPMiddleware): it forwards the pass
    # lane untouched, so the streaming /{name}/mcp transport and its DNS-rebinding guard are
    # unaffected. See gecko.waf for the block-vs-breadcrumb-vs-serve lanes.
    middleware = [Middleware(WafMiddleware, public_url=public_url)]
    return Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)


def security_allowlist(
    host: str,
    port: int,
    extra_hosts: list[str] | None = None,
    extra_origins: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Compute the Host/Origin allowlists for the bind address + any tunnel hostnames.

    A public HTTPS tunnel (cloudflared/ngrok) presents its own ``Host``; the founder
    adds it via ``extra_hosts``/``extra_origins`` so the rebinding guard still passes.
    """
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"{host}:{port}"}
    hosts.update(extra_hosts or [])
    origins: set[str] = set(extra_origins or [])
    for h in hosts:
        origins.add(f"http://{h}")
        origins.add(f"https://{h}")
    return sorted(hosts), sorted(origins)


def _uvicorn_kwargs(host: str, port: int) -> dict[str, Any]:
    """The uvicorn.run kwargs, factored out of the live-smoke serve functions so the
    proxy-header policy is unit-testable (the serve functions themselves are pragma:
    no cover).

    ``proxy_headers=True`` + ``forwarded_allow_ips`` make uvicorn trust the ALB's
    ``X-Forwarded-For`` so ``client.host`` (and the access log) becomes the REAL client
    IP, not the ALB internal IP — enabling attribution / rate-limiting / honeypot
    fingerprinting. The client IP stays in network metadata (never a body/arg log).
    """
    return {
        "host": host,
        "port": port,
        "proxy_headers": True,
        "forwarded_allow_ips": os.environ.get(FORWARDED_ALLOW_IPS_ENV, "*"),
    }


def serve_http(
    spec_or_client: Any,
    host: str = "127.0.0.1",
    port: int = 8000,
    mode: CallMode = "recorded",
    *,
    base_url: str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    public_url: str | None = None,
    enforce: EnforceMode | None = None,
) -> None:  # pragma: no cover - exercised by the founder-run live smoke
    """Serve the surface over Streamable HTTP via uvicorn. Blocks until stopped.

    ``enforce`` resolves through ``resolve_hosted_enforce`` — the SAME hosted default
    (block) the multi-surface server uses, so single- and multi-surface hosting can never
    diverge on the gate stance (the reviewer's serve_http→warn vs multi→block bug)."""
    import uvicorn

    hosts, origins = security_allowlist(host, port, allowed_hosts, allowed_origins)
    app = build_http_app(
        spec_or_client,
        base_url=base_url,
        mode=mode,
        server_name=server_name,
        allowed_hosts=hosts,
        allowed_origins=origins,
        public_url=public_url,
        enforce=resolve_hosted_enforce(enforce),
    )
    uvicorn.run(app, **_uvicorn_kwargs(host, port))


def serve_multi_http(
    surfaces: list[tuple[str, Any]],
    host: str = "127.0.0.1",
    port: int = 8000,
    mode: CallMode = "recorded",
    *,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    public_url: str | None = None,
    enforce: EnforceMode | None = None,
    registry_routes: list[Any] | None = None,
    background_tasks: list[Callable[[], Coroutine[Any, Any, None]]] | None = None,
) -> None:  # pragma: no cover - exercised by the founder-run live smoke
    """Serve MANY surfaces from one host via uvicorn (each under /{name}). Blocks.

    ``enforce`` is threaded to the risk gate on every surface; ``None`` uses the hosted
    ``block`` default (see ``build_multi_surface_app``). ``registry_routes`` and
    ``background_tasks`` are forwarded unchanged (see ``build_multi_surface_app``)."""
    import uvicorn

    hosts, origins = security_allowlist(host, port, allowed_hosts, allowed_origins)
    app = build_multi_surface_app(
        surfaces,
        mode=mode,
        allowed_hosts=hosts,
        allowed_origins=origins,
        public_url=public_url,
        enforce=enforce,
        registry_routes=registry_routes,
        background_tasks=background_tasks,
    )
    uvicorn.run(app, **_uvicorn_kwargs(host, port))
