"""Container entrypoint — serve the humanitarian MCP surfaces over Streamable HTTP.

Binds ``0.0.0.0:$PORT`` behind the ALB, ``mode="live"`` (real upstream calls). Serves
MANY comprehended surfaces from one host — each under ``/{name}`` — so an agent adds
``/{name}/mcp`` and finds ``/{name}/llms.txt``, and everyone finds everything on one
server. ``/`` lists what's available; ``/healthz`` is the ALB target.

Most surfaces are public (no auth). One (``refugios``) is gated by a PUBLISHABLE
Supabase ``apikey`` (public by design) — served only when ``REFUGIOS_APIKEY`` is set,
via a static-header session that injects the key at call time (hidden from the agent).
No key ⇒ that surface is simply not served; the repo carries no key.

Every host also exposes the 'submit your API' front doors (wired in by
``build_multi_surface_app``): ``POST /comprehend`` (human page backend, also directly
agent-POST-able) and the ``comprehend_api`` MCP tool at ``/gecko/mcp``. A provider
submits one URL and gets it comprehended into first-call-correct tools, returned to the
submitter only — never hosted or added to ``/`` (no public catalog).

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

from .access import public_session, static_session, stub_session
from .client import AgentApiClient
from .enforce import resolve_hosted_enforce
from .http_server import serve_multi_http
from .mcp_server import McpSurface
from .registry.api import registry_routes as _registry_routes
from .registry.store import RegistrySurface, SurfaceStore

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

# Auth-gated surface: served only when its publishable key is present in the env.
_REFUGIOS_SPEC = _ROOT / "examples" / "refugios_demo" / "spec" / "refugios_openapi.json"

# Gecko-brand DEMO surfaces — paid / mainnet-money APIs (TxLINE, Jito) that we can't
# serve live publicly. Served in RECORDED mode: every response is synthesized from the
# schema ($0, offline), no real credential is used or exposed. The point is to show
# first-call-correct comprehension of the exact APIs Gecko pitches; live data needs the
# caller's own subscription. TxLINE uses a stub session so its fully auth-gated ops are
# still visible as tools (recorded mode never sends the stub header anywhere); Jito's
# JSON-RPC methods are public.
_TXLINE_SPEC = _ROOT / "examples" / "txline_demo" / "spec" / "txline_openapi.yaml"
_JITO_SPEC = _ROOT / "examples" / "jito_demo" / "spec" / "jito_openapi.json"

PUBLIC_HOST = "mcp.geckovision.tech"
PUBLIC_URL = f"https://{PUBLIC_HOST}"


def main() -> None:  # pragma: no cover - run-the-server entrypoint
    port = int(os.environ.get("PORT", "8000"))
    # Hosted default resolved in the ONE shared place (resolve_hosted_enforce): block,
    # unless GECKO_ENFORCE dials it down (needs a redeploy).
    hosted_enforce = resolve_hosted_enforce()
    surfaces: list[tuple[str, Any]] = [
        (name, json.loads(path.read_text("utf-8"))) for name, path in _SURFACES
    ]
    # Registry store for the /registry/... HTTP surface — same specs this host already
    # serves, all "free" (no entitlement gate yet). Keys wired with real Mongo in Task 6;
    # keys=None here means every registry fetch is anonymous-free / 402-on-premium-never.
    registry_store = SurfaceStore(
        [RegistrySurface(name=n, spec=s, tier="free") for n, s in surfaces]
    )
    # Recorded brand demo surfaces (pre-built so their mode overrides the host default).
    # Built with the hosted enforce stance so the risk gate is active on them too.
    surfaces.append(
        (
            "txline",
            McpSurface(
                AgentApiClient(str(_TXLINE_SPEC), session=stub_session()),
                mode="recorded",
                enforce=hosted_enforce,
            ),
        )
    )
    surfaces.append(
        (
            "jito",
            McpSurface(
                AgentApiClient(str(_JITO_SPEC), session=public_session()),
                mode="recorded",
                enforce=hosted_enforce,
            ),
        )
    )
    # Refugios (shelters) — comprehended with the publishable apikey injected as a
    # static header. Passed as a CLIENT (not a bare spec) so the multi-surface builder
    # uses its session; the key is invisible to the agent.
    refugios_key = os.environ.get("REFUGIOS_APIKEY", "").strip()
    if refugios_key:
        surfaces.append(
            (
                "refugios",
                AgentApiClient(
                    str(_REFUGIOS_SPEC),
                    session=static_session({"apikey": refugios_key}),
                ),
            )
        )
    serve_multi_http(
        surfaces,
        host="0.0.0.0",  # noqa: S104 - bind all interfaces; the ALB fronts it
        port=port,
        mode="live",
        allowed_hosts=[PUBLIC_HOST],
        public_url=PUBLIC_URL,
        enforce=hosted_enforce,
        registry_routes=_registry_routes(registry_store, None),
    )


if __name__ == "__main__":
    main()
