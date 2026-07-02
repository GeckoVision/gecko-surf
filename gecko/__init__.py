"""Gecko — make any API agent-usable without integration code.

V1 = the comprehension layer: ingest a human-shaped OpenAPI surface and emit
question-shaped, first-call-correct agent tools. No data is ingested — only the
API's public capability surface (endpoints, params, schemas). The agent calls
the upstream API directly for data.

The names re-exported here are the **stable SDK surface** a builder embeds:

    from gecko import AgentApiClient, public_session
    client = AgentApiClient(spec, session=public_session())
    client.call(tool, args, mode="recorded")  # $0, offline, falsifiable

Everything here resolves engine-only — no `serve`/`sosbot` extra needed to import.
The MCP transport (mcp/uvicorn/starlette) stays in `gecko.http_server`/`serve`,
imported lazily by those modules so `import gecko` stays dep-light.
"""

from __future__ import annotations

# Defined BEFORE the submodule imports below: telemetry.py does `from . import
# __version__` at import time, and it is now pulled into the client import chain
# (client -> events -> telemetry), so the name must already exist to avoid a
# partially-initialized-module circular import.
__version__ = "0.1.0"

from .access import NoAuthSession, Session, public_session
from .client import AgentApiClient
from .mcp_server import McpSurface

__all__ = [
    "AgentApiClient",
    "McpSurface",
    "Session",
    "NoAuthSession",
    "public_session",
    "__version__",
]
