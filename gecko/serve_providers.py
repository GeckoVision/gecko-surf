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

**Money-boundary (Jito):** Jito's reads are served live, but its two money-moving writes
(sendBundle, sendTransaction) are catalog-only (recorded) — we are the catalog, not the
relay. That split lives in ``gecko.jito_surface`` so this host and ``serve_mcp`` can't
diverge on it.

Same image as ``serve_mcp``; deployed as a SECOND service with this module as the
entrypoint and ``GECKO_PROVIDER_HOST`` set to the provider host. Thin by design.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .enforce import resolve_hosted_enforce
from .http_server import serve_multi_http
from .jito_surface import build_jito_surface

# In the image: /app/gecko/serve_providers.py -> parents[1] = /app (repo root).
_ROOT = Path(__file__).resolve().parents[1]

# Provider surfaces served on this host. Public reads; each provider keeps its own
# endpoint — agents reach the provider directly.
_PEGANA_SPEC = _ROOT / "examples" / "pegana_demo" / "spec" / "pegana_openapi.json"

# The provider host — set to the real domain at deploy time (DNS-rebinding allowlist).
PROVIDER_HOST = os.environ.get("GECKO_PROVIDER_HOST", "providers.geckovision.tech")
PROVIDER_URL = f"https://{PROVIDER_HOST}"


def _build_surfaces() -> list[tuple[str, Any]]:
    """The surfaces this host serves — factored out of ``main()`` so tests can assert
    the construction offline (a live multi-server surface MUST carry an explicit pin).

    Jito is built via the SHARED boundary builder (``gecko.jito_surface``), the single
    source of truth so this host and ``serve_mcp`` enforce the identical split: its READ
    ops go LIVE against the pinned mainnet Block Engine, while its two money-moving WRITE
    ops (sendBundle, sendTransaction) stay RECORDED — catalog-only, never relayed. That
    matches this host's stated 'public reads' contract; without the builder the bare-live
    client would have served those money-movers live (an open MEV relay)."""
    hosted_enforce = resolve_hosted_enforce()
    return [
        ("pegana", json.loads(_PEGANA_SPEC.read_text("utf-8"))),
        ("jito", build_jito_surface(hosted_enforce)),
    ]


def main() -> None:  # pragma: no cover - run-the-server entrypoint
    port = int(os.environ.get("PORT", "8000"))
    surfaces = _build_surfaces()
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
