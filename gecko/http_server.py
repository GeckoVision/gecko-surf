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
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import corpus, keyauth
from .access import public_session
from .caller import CallError
from .agentnative import build_artifacts
from .client import AgentApiClient
from .keyauth import KeyGate
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

# The hosted-login endpoints (email → OTP → minted Gecko key). Both bodies are tiny JSON
# envelopes ({"email"} / {"login_id","code"}); cap hard before parsing (unauthenticated door).
LOGIN_START_PATH = "/auth/login/start"
LOGIN_VERIFY_PATH = "/auth/login/verify"
MAX_LOGIN_REQUEST_BYTES = 4 * 1024

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
# The closed wire set: `gecko add --mode` offers recorded|live, and "serve" is the
# `gecko serve` first-run ping (the /make-agent-ready channel). Deliberately NARROWER
# than modes.CALL_MODES ("probe" is an engine mode, never an onboard). Keep this in
# lockstep with the client (onboard.send_onboard_ping / send_serve_ping) in the SAME
# commit — a client mode this set lacks 204s but silently emits nothing.
_ONBOARD_PING_MODES: frozenset[str] = frozenset({"recorded", "live", "serve"})


def parse_onboard_ping(body: bytes) -> dict[str, str] | None:
    """Strictly validate an onboard-ping body; ``None`` on ANY deviation (fail closed).

    A valid body is a small JSON object carrying EXACTLY ``ONBOARD_PING_KEYS``, every
    value a non-empty string of at most ``_MAX_ONBOARD_VALUE`` chars, and ``mode`` from
    the closed recorded|live|serve set. Junk JSON, an unknown/missing key, an oversized
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


# The env switch that turns the Gecko-key gate on for a hosted deploy (Layer 1). OFF
# unless explicitly truthy — every keyless/public surface must behave byte-identically
# by default (the critical regression). A redeploy is required to flip it.
REQUIRE_GECKO_KEY_ENV = "GECKO_REQUIRE_KEY"
_TRUE = frozenset({"1", "true", "yes", "on"})


# WHICH surfaces the gate applies to (comma-separated names). The gate stance
# (GECKO_REQUIRE_KEY) is host-wide; this narrows it to the PAID surfaces so the public
# funnel (humanitarian + keyless demos) is never closed by turning the gate on.
GATED_SURFACES_ENV = "GECKO_GATED_SURFACES"


def resolve_require_gecko_key(explicit: bool | None = None) -> bool:
    """Resolve the gate stance: explicit wins, else ``GECKO_REQUIRE_KEY``, else OFF."""
    if explicit is not None:
        return explicit
    return os.environ.get(REQUIRE_GECKO_KEY_ENV, "").strip().lower() in _TRUE


def resolve_gated_surfaces(
    explicit: Iterable[str] | None = None,
    *,
    default: frozenset[str] | None = None,
) -> frozenset[str] | None:
    """Resolve WHICH surface names the Gecko-key gate applies to.

    Explicit wins, else ``GECKO_GATED_SURFACES`` (comma-separated), else ``default``.

    ``None`` means **every** mount is gated — the pre-existing behavior, kept as the
    library default so no other caller silently changes. The hosted server passes its own
    ``default`` (``serve_mcp.GATED_SURFACES``) so only the paid surfaces are gated there.
    An empty/whitespace env value is treated as unset (fall back to ``default``) — the
    safe direction, since the fallback can only ever gate MORE, never less.

    **Fail closed on garbage.** A value that is non-empty but parses to ZERO names
    (``","``, ``",,,"``) used to yield an empty set — i.e. the gate stayed ON while gating
    NOTHING, silently leaving a PAID surface open. Such a value now falls back to
    ``default`` and is logged at ERROR: the fallback can only ever gate more, and the
    operator's mistake is visible instead of invisible.
    """
    if explicit is not None:
        return frozenset(explicit)
    raw = os.environ.get(GATED_SURFACES_ENV, "").strip()
    if not raw:
        return default
    names = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        logger.error(
            "%s=%r names no surface — falling back to the default gated set (fail closed)",
            GATED_SURFACES_ENV,
            raw,
        )
        return default
    return names


def _bearer_from_scope(scope: Any) -> str | None:
    """Pull the Gecko key from an ASGI scope's ``Authorization: Bearer <token>`` header.

    Header-only (never the body), so the gate never consumes/blocks the streaming MCP
    transport. UNTRUSTED input — the value is passed straight to the injected resolver
    and NEVER logged. Absent/malformed ⇒ ``None`` (which the gate denies)."""
    for key, value in scope.get("headers") or []:
        if bytes(key).lower() == b"authorization":
            try:
                raw = bytes(value).decode("latin-1").strip()
            except Exception:  # noqa: BLE001 - a malformed header is simply no key
                return None
            scheme, _, token = raw.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


class _GeckoKeyGateASGI:
    """Access-control edge for one served MCP mount: verify the Gecko key + allowlist
    (``keyauth.authorize``) and 403 everyone else, otherwise pass straight through.

    Applied ONLY when a :class:`~gecko.keyauth.KeyGate` is wired (opt-in). It reads just
    the ``Authorization`` header from the scope — never the body — so the streaming
    transport, its DNS-rebinding guard, and the funnel wrapper it fronts are untouched
    on the allow path. A denial NEVER echoes the token (redact-before-raise); the JSON
    body carries the reason only. Non-HTTP scopes pass through unchanged.
    """

    def __init__(self, inner: Any, gate: KeyGate) -> None:
        self._inner = inner
        self._gate = gate

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return
        decision = self._gate.decide(_bearer_from_scope(scope))
        if decision.allowed:
            await self._inner(scope, receive, send)
            return
        # Denied: a clean 403 with the REASON only. The account/token never appears in
        # the body, and the log line names the reason (never the key).
        logger.info("gecko-key gate denied (reason=%s)", decision.reason)
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "gecko key required", "reason": decision.reason},
            status_code=403,
        )
        await response(scope, receive, send)


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


def _user_agent_from_context(server: Any) -> str | None:
    """Best-effort read of the HTTP ``User-Agent`` from the low-level server's per-request
    context (same seam as ``_session_id_from_context``). UNTRUSTED — the caller sanitizes
    it via ``_safe_user_agent`` before it is ever stored. ``None`` outside a request or when
    the header is absent."""
    try:
        ctx = server.request_context
    except LookupError:
        return None
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("user-agent")
    except Exception:  # noqa: BLE001 - a non-mapping request object is simply no UA
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
    gate: KeyGate | None = None,
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

    ``gate`` (Layer 1 access control) wraps the ``/mcp`` mount with the Gecko-key auth
    edge (``keyauth``). It is **off by default** (``None``): when ``None`` the ``/mcp``
    route is byte-identical to before — no wrapper, no behavior change on any keyless/
    public surface. When set, an unauthorized request gets a clean 403 and only an
    enabled Gecko key passes through to the existing handler.
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
        # Per-request (not build-time): so the funnel sees a tools/list per real session,
        # and McpSurface.list_tools emits surf.list_tools joined to this session's connect.
        # The projection is cheap (<50 ops on every hosted surface -> byte-identical branch).
        if isinstance(surface, McpSurface):
            user_agent = _safe_user_agent(_user_agent_from_context(server))
            tools = surface.list_tools(
                session_id=_session_id_from_context(server),
                user_agent=user_agent,
                # clientInfo is only in the initialize frame, not on this request; classify
                # by UA alone (same UA-only stance as the non-init connect_failed path).
                client_kind=classify_client(user_agent, None),
            )
        else:
            tools = (
                surface.list_tools()
            )  # duck-typed meta surface: no correlation kwargs
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
    # Layer 1 access control (opt-in): when a gate is wired, front the mount with the
    # Gecko-key edge so unauthorized keys 403 before reaching the transport/funnel. When
    # `gate is None` (default) `mcp_app is asgi_app`, so the route below is byte-identical.
    mcp_app = asgi_app if gate is None else _GeckoKeyGateASGI(asgi_app, gate)

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
            Route(MCP_PATH, endpoint=mcp_app),
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
    require_gecko_key: bool | None = None,
    gated_surfaces: Iterable[str] | None = None,
    key_gate: KeyGate | None = None,
    key_registry: Any | None = None,
    login_service: Any | None = None,
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

    ``require_gecko_key`` (Layer 1 access control) gates the per-surface ``/{name}/mcp``
    mounts behind a Gecko key + founder allowlist. It resolves explicit → ``GECKO_REQUIRE_KEY``
    → OFF, so a keyless deploy is byte-identical to before. The **public submit door**
    (``/comprehend`` + the meta ``/gecko/mcp``) stays open — it is the front door, not a
    paid surface. ``key_gate`` injects the verifier+allowlist seam; when the gate is on
    but none is given, a fail-closed gate (deny everyone) is used rather than fail-open.

    ``gated_surfaces`` narrows WHICH mounts that gate applies to (explicit →
    ``GECKO_GATED_SURFACES`` → ``None``). ``None`` gates every mount — the pre-existing
    behavior, kept as the default so no caller changes silently. The hosted server passes
    the paid set (``serve_mcp.GATED_SURFACES``) so a PAID third-party API can be closed to
    named developers while the humanitarian + keyless demo surfaces — the funnel — stay
    open. A name here that isn't served is simply inert. Fail-closed is preserved per
    surface: a gated mount with no usable resolver denies; ungated mounts are untouched.

    A gated surface is gated at the **Mount**, not just at ``/mcp``: its discovery
    siblings (``tools.md`` / ``llms.txt`` / ``SKILL.md`` / ``gecko.json`` /
    ``.well-known/gecko.json``) carry the whole comprehension artifact, and gating only
    ``/mcp`` served them in the clear. It is also withheld from the anonymous root index
    and the ``.well-known`` manifests (a valid key still sees it — same gate decision).
    An UNGATED mount is appended as the raw sub-app, with no wrapper object anywhere.

    ``key_registry`` (the Gecko-key registry, ``gecko.keyregistry``) supersedes the Privy-JWT
    resolver on the gate: when the gate is on and a registry is wired (explicit or via
    ``MONGODB_URI``), the per-surface mounts verify minted ``gecko_sk_…`` keys against it.
    ``login_service`` (``gecko.authlogin.LoginService``) powers the ``/auth/login/*`` endpoints
    below; when neither it nor its env config is present, those routes answer a clean 503. Both
    are injected in tests to stay fully offline.
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

    # Layer 1: resolve the gate stance once (explicit → env → OFF). When on and no gate
    # is injected, wire the REAL Privy verifier when Privy is configured (PRIVY_APP_ID),
    # else fall back to a fail-closed resolver (deny everyone) so an un-configured deploy
    # never fails open. When off, `surface_gate` stays None → per-surface mounts unchanged.
    surface_gate: KeyGate | None = None
    if resolve_require_gecko_key(require_gecko_key):
        from .keyauth import AccountResolver, FileAllowlist, deny_all_resolver

        if key_gate is not None:
            surface_gate = key_gate
        else:
            from .keyregistry import (
                GeckoKeyResolver,
                RegistryAllowlist,
                registry_from_env,
            )

            registry = key_registry or registry_from_env()
            if registry is not None:
                # Gecko-key registry configured: verify minted gecko_sk_… keys (enabled lives
                # on the registry record) — supersedes the Privy-JWT resolver on this seam.
                surface_gate = KeyGate(
                    resolve_account=GeckoKeyResolver(registry),
                    allowlist=RegistryAllowlist(registry),
                )
            else:
                from .privy_auth import privy_resolver_from_env

                resolver: AccountResolver = (
                    privy_resolver_from_env() or deny_all_resolver
                )
                surface_gate = KeyGate(
                    resolve_account=resolver, allowlist=FileAllowlist()
                )

    # WHICH mounts that gate applies to. None = every mount (the pre-existing behavior);
    # the hosted server names the paid surfaces so the public funnel stays open.
    gated_names = resolve_gated_surfaces(gated_surfaces)
    # Match case-INSENSITIVELY: mount names are lowercase by convention, and a casing slip
    # in GECKO_GATED_SURFACES ("BIRDEYE") silently left the PAID mount open. Folding can
    # only ever gate MORE surfaces, never fewer, so it is safe in the fail-closed direction.
    gated_folded = None if gated_names is None else {n.casefold() for n in gated_names}

    # A gated name this host does not serve is inert BY DESIGN (it lets an operator
    # forward-declare a future paid surface) — but inert must never be SILENT: a typo
    # ("birdye") is indistinguishable from a correct config on the wire, and the end state
    # it produces (gate ON, every mount OPEN) is exactly the failure this gate exists to
    # prevent. Loud at ERROR so the deploy log shows it; deliberately not fatal, so a typo
    # cannot take the humanitarian/public mounts down with it.
    if (
        surface_gate is not None
        and gated_names is not None
        and gated_folded is not None
    ):
        served = {name.casefold() for name, _ in surfaces}
        unserved = sorted(n for n in gated_names if n.casefold() not in served)
        if unserved:
            logger.error(
                "gecko-key gate names surfaces this host does not serve: %s "
                "(typo? they gate NOTHING) — served: %s",
                unserved,
                sorted(name for name, _ in surfaces),
            )
        if not (served & gated_folded):
            logger.error(
                "gecko-key gate is ON but gates NO served surface — every mount is OPEN. "
                "Check %s.",
                GATED_SURFACES_ENV,
            )

    subs: list[tuple[str, Starlette]] = []
    #: The mounts that actually carry a gate — i.e. the PAID surfaces, and only when the
    #: gate is ON. Drives the Mount-level wrapping below AND what an anonymous caller is
    #: told exists (index / .well-known). Empty ⇒ every route below is unchanged.
    gated_mounts: set[str] = set()
    for name, spec in surfaces:
        if registry_routes and name == "registry":
            raise ValueError(
                "surface name 'registry' is reserved (would shadow /registry/*)"
            )
        site = f"{public_url.rstrip('/')}/{name}" if public_url else None
        # Per-surface gate selection. `surface_gate` is None when the gate is OFF; when it
        # is ON it is either the real verifier or the fail-closed deny-all one — so a
        # NAMED (paid) surface can never fail open, and an unnamed one is never closed.
        if surface_gate is not None and (
            gated_folded is None or name.casefold() in gated_folded
        ):
            gated_mounts.add(name)
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
                    # NOT gated here: the gate goes around the whole Mount below, so it
                    # covers the discovery siblings (tools.md / llms.txt / SKILL.md /
                    # gecko.json) too — gating only `/mcp` served the ENTIRE comprehension
                    # artifact of a paid surface in the clear (R1).
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

    # Comprehended surfaces served on this host (NOT a public marketplace listing —
    # each is a spec the operator chose to serve). Submissions are never added here.
    surface_entries: list[dict[str, str]] = [
        {
            "name": name,
            "mcp": f"{public_url.rstrip('/')}/{name}/mcp"
            if public_url
            else f"/{name}/mcp",
            "llms_txt": f"/{name}/llms.txt",
        }
        for name, _ in subs
    ]

    index = {
        "name": "gecko",
        "description": "Comprehended API surfaces, served agent-native.",
        "surfaces": surface_entries,
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

    # A gated (paid) surface is not ADVERTISED to anonymous callers: the root index and
    # the host manifests used to name it, which both hands a scraper the paid catalog and
    # drifts us toward the marketplace listing the thesis forbids. A caller holding a
    # valid key still sees it (same gate decision as the mount — one source of truth), so
    # discovery is not broken for the developers it was opened to. With no gated mount
    # both dicts are the SAME object, so every public deploy is byte-identical.
    _public_index = (
        index
        if not gated_mounts
        else {
            **index,
            "surfaces": [e for e in surface_entries if e["name"] not in gated_mounts],
        }
    )

    def _sees_gated(scope: Any) -> bool:
        if not gated_mounts or surface_gate is None:
            return True
        return surface_gate.decide(_bearer_from_scope(scope)).allowed

    async def _healthz(_request: Any) -> Any:
        return PlainTextResponse("ok")

    async def _index(request: Request) -> Any:
        return JSONResponse(index if _sees_gated(request.scope) else _public_index)

    async def _mcp_root_redirect(_request: Any) -> Any:
        # /mcp is the conventional default path a real MCP client tries; it lives only
        # at /{name}/mcp and /gecko/mcp, so a bare POST /mcp used to 404 (silent
        # onboarding failure). 307 preserves method+body and points at the meta front
        # door. Whether a given MCP client auto-follows a 307 on POST is the live-smoke
        # check (Pattern B): httpx/fetch follow by default, but the founder confirms it.
        return RedirectResponse(url=f"/{META_SURFACE_NAME}{MCP_PATH}", status_code=307)

    async def _well_known_gecko(request: Request) -> Any:
        # Host-level discovery — the SAME content _index returns (surfaces + submit door).
        return JSONResponse(index if _sees_gated(request.scope) else _public_index)

    async def _well_known_x402(request: Request) -> Any:
        # Honest, control-plane-safe x402 stance: Gecko composes x402, custody none.
        # Gated surfaces are withheld from anonymous callers on the same rule as the index.
        visible = (
            surfaces
            if _sees_gated(request.scope)
            else [(n, s) for n, s in surfaces if n not in gated_mounts]
        )
        return JSONResponse(build_x402_manifest(visible, public_url))

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

    # Hosted login (server-side identity → minted Gecko key). Resolve the service once:
    # injected wins, else env-wired (Privy secret + registry), else the endpoints 503.
    from .authlogin import LoginServiceError, build_login_service_from_env

    _login_svc = (
        login_service if login_service is not None else build_login_service_from_env()
    )

    async def _login_body(request: Request) -> dict[str, Any] | None:
        # Size-cap before AND after reading (same convention as _comprehend); None on junk.
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit():
            if int(declared) > MAX_LOGIN_REQUEST_BYTES:
                return None
        raw = await request.body()
        if len(raw) > MAX_LOGIN_REQUEST_BYTES:
            return None
        try:
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def _login_start(request: Request) -> Any:
        if _login_svc is None:
            return JSONResponse({"error": "login_disabled"}, status_code=503)
        body = await _login_body(request)
        if body is None:
            return JSONResponse({"error": "invalid request"}, status_code=400)
        try:
            login_id = _login_svc.start(str(body.get("email", "")), _client_ip(request))
        except LoginServiceError as exc:
            # The message is redacted by construction (never a code/secret).
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        return JSONResponse({"login_id": login_id})

    async def _login_verify(request: Request) -> Any:
        if _login_svc is None:
            return JSONResponse({"error": "login_disabled"}, status_code=503)
        body = await _login_body(request)
        if body is None:
            return JSONResponse({"error": "invalid request"}, status_code=400)
        try:
            api_key = _login_svc.verify(
                str(body.get("login_id", "")),
                str(body.get("code", "")),
                _client_ip(request),
            )
        except LoginServiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        # The minted key is returned EXACTLY ONCE. It is never logged here or anywhere.
        return JSONResponse({"api_key": api_key})

    routes: list[Any] = [
        Route("/healthz", endpoint=_healthz),
        Route("/", endpoint=_index),
        Route(LOGIN_START_PATH, endpoint=_login_start, methods=["POST"]),
        Route(LOGIN_VERIFY_PATH, endpoint=_login_verify, methods=["POST"]),
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
        # Gate the WHOLE mount for a paid surface — `/mcp` and every discovery sibling
        # (tools.md / llms.txt / SKILL.md / gecko.json / .well-known/gecko.json), which
        # carry the full comprehension artifact. Denials stay the same clean 403 the
        # `/mcp` edge already returned (one status, one reason vocabulary). An UNGATED
        # mount is appended exactly as before — the raw sub-app, no wrapper object.
        routes.append(
            Mount(
                f"/{name}",
                app=sub
                if name not in gated_mounts or surface_gate is None
                # Scoped PER MOUNT: holding a valid enabled key is not enough, the
                # account must also be granted THIS surface. Without the scoping a
                # single key opened every gated surface at once.
                else _GeckoKeyGateASGI(sub, keyauth.scope_gate(surface_gate, name)),
            )
        )
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
    gated_surfaces: Iterable[str] | None = None,
) -> None:  # pragma: no cover - exercised by the founder-run live smoke
    """Serve MANY surfaces from one host via uvicorn (each under /{name}). Blocks.

    ``enforce`` is threaded to the risk gate on every surface; ``None`` uses the hosted
    ``block`` default (see ``build_multi_surface_app``). ``registry_routes``,
    ``background_tasks`` and ``gated_surfaces`` (which mounts the Gecko-key gate applies
    to) are forwarded unchanged (see ``build_multi_surface_app``)."""
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
        gated_surfaces=gated_surfaces,
    )
    uvicorn.run(app, **_uvicorn_kwargs(host, port))
