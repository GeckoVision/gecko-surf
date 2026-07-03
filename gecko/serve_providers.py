"""Provider-facing MCP host — a SEPARATE space from the humanitarian relief host.

Serves comprehended PROVIDER surfaces (Pegana, JITO) so an agent can add ``/{name}/mcp``
and call the provider's OWN API correctly, first try — and every call emits a
control-plane-safe usage event (``surf_events``) so we can measure real per-provider
adoption (the honest metric + the first provider-side signal for the corpus flywheel).

**On-thesis discipline (do not drift):** this is the comprehension/consumption layer
for providers — we serve each provider's OWN surface (agents reach the provider's real
endpoint) and emit a discovery breadcrumb per surface. It is NOT a marketplace: we do
NOT re-list providers as a browsable catalog/directory (that's the distribution layer —
frames.ag / pay.sh — which we compose, never become). ``/`` lists the served surfaces as
a breadcrumb, not a shop.

Same image as ``serve_mcp``; deployed as a SECOND service with this module as the
entrypoint and ``GECKO_PROVIDER_HOST`` set to the provider host. Thin by design.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .http_server import serve_multi_http

# In the image: /app/gecko/serve_providers.py -> parents[1] = /app (repo root).
_ROOT = Path(__file__).resolve().parents[1]

# Provider surfaces served on this host. Public reads; each provider keeps its own
# endpoint (from the spec's servers[].url) — agents reach the provider directly.
_SURFACES: list[tuple[str, Path]] = [
    ("pegana", _ROOT / "examples" / "pegana_demo" / "spec" / "pegana_openapi.json"),
    ("jito", _ROOT / "examples" / "jito_demo" / "spec" / "jito_openapi.json"),
]

# The provider host — set to the real domain at deploy time (DNS-rebinding allowlist).
PROVIDER_HOST = os.environ.get("GECKO_PROVIDER_HOST", "providers.geckovision.tech")
PROVIDER_URL = f"https://{PROVIDER_HOST}"


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
        allowed_hosts=[PROVIDER_HOST],
        public_url=PROVIDER_URL,
    )


if __name__ == "__main__":
    main()
