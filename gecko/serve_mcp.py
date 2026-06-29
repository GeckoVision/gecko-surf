"""Container entrypoint — serve the SOS Venezuela MCP surface over Streamable HTTP.

Binds ``0.0.0.0:$PORT`` behind the ALB, ``mode="live"`` (real upstream calls to
https://sosvenezuela2026.com — the spec's ``servers[0].url``; the API is public, no
auth). ``mcp.geckovision.tech`` is allowlisted for the DNS-rebinding defense: the ALB
preserves that ``Host`` (port-less on 443) on real ``/mcp`` traffic, so the bare
hostname must be present verbatim.

``/healthz`` (added in ``http_server.build_http_app``) is a plain Starlette route that
bypasses the rebinding guard, so the ALB target-group health check (``Host:
<task-ip>:8000``) passes without allowlisting the private IP.

Thin by design — all logic lives in ``gecko.http_server``. Do NOT reuse
``examples/.../serve_sos_mcp.py`` here: that binds ``127.0.0.1`` with no allowlist.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .http_server import serve_http

# In the image: /app/surfcall/serve_mcp.py -> parents[1] = /app (repo root).
SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "sos_vzla_bot"
    / "spec"
    / "sosvenezuela_openapi.json"
)
PUBLIC_HOST = "mcp.geckovision.tech"
SERVER_NAME = "sosvenezuela"


def main() -> None:  # pragma: no cover - run-the-server entrypoint
    port = int(os.environ.get("PORT", "8000"))
    # Local path bypasses the SSRF guard by design (trusted, shipped in-image).
    spec = json.loads(SPEC_PATH.read_text("utf-8"))
    serve_http(
        spec,
        host="0.0.0.0",  # noqa: S104 - bind all interfaces; the ALB fronts it
        port=port,
        mode="live",
        server_name=SERVER_NAME,
        allowed_hosts=[PUBLIC_HOST],
    )


if __name__ == "__main__":
    main()
