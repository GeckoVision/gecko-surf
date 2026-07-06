"""Host-level ``.well-known`` discovery manifests for the public multi-surface app.

An x402-aware probe expects ``/.well-known/x402.json`` at the host root. Gecko
*composes* x402 — it never becomes the payment rail — so this manifest is a truthful
advertisement of that stance, not a settlement endpoint.

**Honesty is the hard requirement (control plane, invariant #1 + #2).** Gecko holds no
funds, signs nothing, and takes no cut. Every surface therefore reports ``payment:
"none"`` unless it *genuinely* carries priced operations, in which case the manifest
points at the PROVIDER'S OWN x402 endpoint (their data, not ours). We never fabricate a
``pay_to`` address, a price, or an endpoint. Nothing is priced today, so everything is
``"none"`` — a real price must flow from the provider's own spec/entitlement data.
"""

from __future__ import annotations

from typing import Any

_X402_NOTE = (
    "Gecko composes x402; payment settles at each provider's own endpoint. "
    "Gecko is not a payment rail, holds no funds, signs nothing, and takes no cut."
)


def _surface_payment(_spec: Any) -> dict[str, str] | str:
    """Return the honest payment descriptor for one served surface.

    Default ``"none"``: no surface is priced today. When a surface genuinely exposes
    priced operations, this must return ``{"endpoint": <the provider's OWN x402
    endpoint>, "scheme": ..., "asset": ...}`` sourced from that provider's spec /
    entitlement data — NEVER a fabricated recipient, price, or endpoint.
    """
    return "none"


def build_x402_manifest(
    surfaces: list[tuple[str, Any]], public_url: str | None
) -> dict[str, Any]:
    """Build the host-level x402 discovery manifest from the served surfaces.

    ``surfaces`` is ``[(name, spec_or_client), ...]`` (the same list the multi-surface
    app mounts). ``public_url`` makes the per-surface MCP URLs absolute; relative when
    omitted. Control-plane safe by construction: only surface names + MCP paths + the
    honest ``payment`` descriptor cross the boundary.
    """
    base = public_url.rstrip("/") if public_url else ""

    def mcp_url(name: str) -> str:
        return f"{base}/{name}/mcp" if base else f"/{name}/mcp"

    return {
        "provider": "gecko",
        "composes": "x402",
        "custody": "none",
        "note": _X402_NOTE,
        "surfaces": [
            {"name": name, "mcp": mcp_url(name), "payment": _surface_payment(spec)}
            for name, spec in surfaces
        ],
    }


# Canonical docs live in Mintlify — the breadcrumb POINTS at them, never duplicates
# the five-move depth (which drifts). One source of truth for onboarding content.
_DOCS_QUICKSTART = "https://docs.geckovision.tech/quickstart"
_DOCS_FOR_PROVIDERS = "https://docs.geckovision.tech/for-providers"


def build_onboard_breadcrumb(public_url: str | None) -> str:
    """Build the served ``/.well-known/onboard.md`` breadcrumb (text/markdown).

    A SHORT signpost for both audiences — a developer who wants to USE an API and a
    provider who wants to ONBOARD one — each pointing at the canonical Mintlify docs.
    It never copies the full onboarding depth; it links to it.

    ``public_url`` makes the served paths absolute (relative when omitted). The path
    constants are imported lazily from ``http_server`` so this stays the single source
    of truth for the routes (and avoids a top-level import cycle).
    """
    # Deferred import: http_server imports this module inside a function, so a lazy
    # import here keeps the routes single-sourced without a cycle.
    from .http_server import COMPREHEND_PATH, MCP_PATH, META_SURFACE_NAME

    base = public_url.rstrip("/") if public_url else ""

    def abs_path(path: str) -> str:
        return f"{base}{path}" if base else path

    add_command = (
        f"claude mcp add --transport http <name> {abs_path('/<name>' + MCP_PATH)}"
    )
    comprehend_url = abs_path(COMPREHEND_PATH)
    meta_mcp = abs_path("/" + META_SURFACE_NAME + MCP_PATH)

    return f"""# Onboard to Gecko

Gecko turns any API's *surface* into first-call-correct agent tools — find the right
call, make it correctly the first time, run. Two ways in:

## Use an API

Add any served surface to your agent and call it correctly on the first try:

```
{add_command}
```

Then call the `search_capabilities` tool to find the right operation, and call it.

Quickstart: {_DOCS_QUICKSTART}

## Onboard your API

Make your own API agent-usable — first-call-correct tools; if you charge, you keep 100%.
Comprehend it self-serve (no account, no cost):

- HTTP: `POST {comprehend_url}`
- MCP tool: `comprehend_api` at {meta_mcp}

For providers: {_DOCS_FOR_PROVIDERS}

---

This is a breadcrumb. The canonical docs are the source of truth: {_DOCS_FOR_PROVIDERS}
"""
