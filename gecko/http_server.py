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

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import corpus
from .access import public_session
from .caller import CallError
from .agentnative import build_artifacts
from .client import AgentApiClient
from .mcp_server import McpSurface

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette

logger = logging.getLogger("gecko.http_server")

DEFAULT_SERVER_NAME = "gecko"
MCP_PATH = "/mcp"

_INSTALL_HINT = (
    "Install the serve extra to run the HTTP server: uv sync --extra serve "
    "(or: uv pip install 'gecko-surf[serve]')"
)


def _surface_from(spec_or_client: Any, base_url: str | None, mode: str) -> McpSurface:
    """Accept a spec (str/dict), an AgentApiClient, or an McpSurface; yield a surface.

    A bare spec is wrapped with a ``public_session`` so auth-gated ops stay hidden —
    M1 is public-only, and the agent must never be offered a tool it can't satisfy.
    """
    if isinstance(spec_or_client, McpSurface):
        return spec_or_client
    if isinstance(spec_or_client, AgentApiClient):
        return McpSurface(spec_or_client, mode=mode)
    client = AgentApiClient(spec_or_client, base_url=base_url, session=public_session())
    return McpSurface(client, mode=mode)


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
    mode: str = "recorded",
    server_name: str = DEFAULT_SERVER_NAME,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    corpus_path: str | Path | None = None,
    surface_id: str | None = None,
    surface_rev: str = "0",
    public_url: str | None = None,
) -> Starlette:
    """Build the Streamable-HTTP ASGI app wrapping ``McpSurface`` (no server run).

    Factored out of ``serve_http`` so tests can mount it in-process (offline) with an
    ASGI transport. ``allowed_hosts``/``allowed_origins`` drive DNS-rebinding defense.

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

    surface = _surface_from(spec_or_client, base_url, mode)
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
        try:
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
    asgi_app = StreamableHTTPASGIApp(manager)

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


def serve_http(
    spec_or_client: Any,
    host: str = "127.0.0.1",
    port: int = 8000,
    mode: str = "recorded",
    *,
    base_url: str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    public_url: str | None = None,
) -> None:  # pragma: no cover - exercised by the founder-run live smoke
    """Serve the surface over Streamable HTTP via uvicorn. Blocks until stopped."""
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
    )
    uvicorn.run(app, host=host, port=port)
