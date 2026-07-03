"""Container entrypoint — serve the humanitarian MCP surfaces over Streamable HTTP.

Binds ``0.0.0.0:$PORT`` behind the ALB, ``mode="live"`` (real upstream calls; every
served API is public, no auth). Serves MANY comprehended surfaces from one host — each
under ``/{name}`` — so an agent adds ``/{name}/mcp`` and finds ``/{name}/llms.txt``,
and everyone finds everything on one server. ``/`` lists what's available; ``/healthz``
is the ALB target.

``mcp.geckovision.tech`` is allowlisted for the DNS-rebinding defense: the ALB preserves
that ``Host`` (port-less on 443) on real ``/mcp`` traffic, so the bare hostname must be
present verbatim.

Thin by design — all logic lives in ``gecko.http_server``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .http_server import serve_multi_http

# In the image: /app/gecko/serve_mcp.py -> parents[1] = /app (repo root).
_ROOT = Path(__file__).resolve().parents[1]

# The surfaces this host serves. Each spec is shipped in-image (a local path bypasses
# the SSRF guard by design — trusted, in-image), comprehended live at startup. Add a
# humanitarian API here + ship its spec to expose it at /{name}/mcp.
_SURFACES: list[tuple[str, Path]] = [
    (
        "reportavnzla",
        _ROOT / "examples" / "reportavnzla_demo" / "spec" / "reportavnzla_openapi.json",
    ),
    (
        "sosvenezuela",
        _ROOT / "examples" / "sos_vzla_bot" / "spec" / "sosvenezuela_openapi.json",
    ),
]

PUBLIC_HOST = "mcp.geckovision.tech"
PUBLIC_URL = f"https://{PUBLIC_HOST}"


def main() -> None:  # pragma: no cover - run-the-server entrypoint
    port = int(os.environ.get("PORT", "8000"))
    surfaces: list[tuple[str, Any]] = [
        (name, json.loads(path.read_text("utf-8"))) for name, path in _SURFACES
    ]
    serve_multi_http(
        surfaces,
        host="0.0.0.0",  # noqa: S104 - bind all interfaces; the ALB fronts it
        port=port,
        mode="live",
        allowed_hosts=[PUBLIC_HOST],
        public_url=PUBLIC_URL,
    )


if __name__ == "__main__":
    main()
