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


def build_server_card(
    surface_names: list[str], public_url: str | None
) -> dict[str, Any]:
    """The MCP server card at /.well-known/mcp/server-card.json — the path
    agent-readiness scanners and indexers actually probe (~70% of this host's
    connects are indexers, and until this existed they indexed nothing).

    One card for the host, remotes per PUBLIC surface. The caller passes the
    already-gate-filtered name list, so this can never leak a gated mount the
    index would withhold. Branding fields (title/iconUrl) because a name+icon+
    description trio is what registries render as a complete listing.

    ``protocolVersion`` and ``authentication`` let a client decide compatibility
    BEFORE opening a session (the Apify card does this; ours did not, 2026-09-01).
    The protocol version is the SDK's own constant, never a literal, so the card
    cannot claim a version the transport does not speak. ``name`` is the product's
    registry name: one name whether the card is read on this host or mirrored on
    the landing.
    """
    from mcp.types import LATEST_PROTOCOL_VERSION

    from . import __version__
    from .mcp_server import MetaComprehendSurface

    base = public_url.rstrip("/") if public_url else ""
    prm_path = "/.well-known/oauth-protected-resource"
    # The host root's own tools (comprehend + surface discovery) — real, callable at
    # serverUrl. Per-surface tools live behind each remote and are not flattened here.
    root_tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "annotations": t.get("annotations"),
        }
        for t in MetaComprehendSurface().list_tools()
    ]
    return {
        "name": "tech.geckovision/gecko",
        "title": "Gecko",
        "description": (
            "Comprehended API surfaces served agent-native over Streamable "
            "HTTP: first-call-correct tools, auth injected server-side, "
            "refusals that say why."
        ),
        "version": __version__,
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "serverUrl": f"{base}/mcp" if base else "/mcp",
        # Open remotes need no credential. A gated surface answers 401 with a
        # WWW-Authenticate that names the PRM below; the PRM carries the self-serve
        # mint path. No OAuth authorization server exists, and none is claimed.
        "authentication": {
            "required": False,
            "schemes": ["bearer"],
            "resource_metadata": f"{base}{prm_path}" if base else prm_path,
        },
        # What an agent can actually DO here, stated up front — a blind-agent test
        # (2026-09-01) showed the words "store"/"purchase" first appeared only inside
        # the initialize response, so an agent tasked with buying had to gamble that
        # this endpoint was relevant.
        "instructions": (
            "Each remote serves one comprehended surface. The orquestra surface "
            "covers Solana commerce and programs: browse storefronts and menus "
            "(list_stores), route a plain intent to the right instruction "
            "(find_start), and prepare a simulation-checked unsigned purchase "
            "(prepare_purchase) that you sign with your own wallet. The root "
            "serverUrl comprehends any OpenAPI URL (comprehend_api) and lists "
            "the mounted surfaces (list_surfaces). No key is needed for the "
            "open surfaces; gated ones answer 401 with the self-serve mint path."
        ),
        "transport": "streamable-http",
        "icon": "https://geckovision.tech/gecko-icon.svg",
        "iconUrl": "https://geckovision.tech/gecko-icon.svg",
        "websiteUrl": "https://geckovision.tech",
        "tools": root_tools,
        "remotes": [
            {
                "transport_type": "streamable-http",
                "url": f"{base}/{name}/mcp" if base else f"/{name}/mcp",
                "name": name,
            }
            for name in surface_names
        ],
    }


#: Natural-language queries a discovery service can vector-match against. ARD asks
#: for 2-5 per entry; these describe what a surface is actually FOR, in the words
#: someone would use before they know our vocabulary. Surfaces without an entry
#: fall back to the generic pair below — never a fabricated claim about the surface.
_REPRESENTATIVE_QUERIES: dict[str, list[str]] = {
    "orquestra": [
        "buy something onchain with USDC on Solana",
        "what can I order from this Solana storefront",
        "derive the accounts for a Solana instruction I have never called",
        "check what this transaction will cost before I sign it",
    ],
}

_GENERIC_QUERIES = [
    "call this API correctly on the first try",
    "which operation of this API answers my question",
]


def build_ard_catalog(
    surface_names: list[str], public_url: str | None
) -> dict[str, Any]:
    """The Agentic Resource Discovery manifest (agenticresourcediscovery.org).

    ARD sits BEFORE invocation: a client asks "what is available for this task?"
    and a discovery service answers with matching resources. So each entry says
    what the surface is for in plain language, and points at the MCP endpoint the
    client then speaks to natively.

    The caller passes the already-gate-filtered names, so this can never advertise
    a mount the index withholds — one withholding rule, now four doors.

    Served at BOTH `/.well-known/ard.json` (the path the ARD spec defines) and
    `/.well-known/ai-catalog.json` (the path readiness scanners actually probe
    while citing that same spec). One payload, two names, because being
    discoverable is the whole point of the file.
    """
    base = public_url.rstrip("/") if public_url else ""
    host = base.split("://", 1)[-1] if base else "geckovision.tech"

    entries = [
        {
            # urn:air:<domain>:<namespace>:<name>, the domain-anchored URN ARD requires.
            "identifier": f"urn:air:{host}:mcp:{name}",
            "displayName": f"Gecko — {name}",
            "type": "application/mcp-server+json",
            "url": f"{base}/{name}/mcp" if base else f"/{name}/mcp",
            "description": (
                "A comprehended API surface served as first-call-correct MCP tools. "
                "Auth is injected server-side; a call that cannot be built correctly "
                "is refused with the reason rather than guessed."
            ),
            # `capabilities` is omitted, not emptied. The per-surface tool list lives
            # behind each remote and is not flattened here, and an empty array would
            # claim this surface has no capabilities — a fabrication where the honest
            # answer is "ask the endpoint". A client reads tools/list from `url`.
            "representativeQueries": _REPRESENTATIVE_QUERIES.get(
                name, _GENERIC_QUERIES
            ),
        }
        for name in surface_names
    ]
    return {"entries": entries}


def build_host_llms_txt(surface_names: list[str], public_url: str | None) -> str:
    """The host-root ``/llms.txt`` — the breadcrumb an agent reads FIRST.

    Per-surface ``llms.txt`` files describe one comprehended API; this one
    describes the host and routes to them. It leads with WHEN TO USE THIS,
    because an agent that lands here from a search result needs to decide
    relevance before it decides anything else — a readiness scan called that
    out, and generic marketing copy does not read as guidance.

    The boundary is stated as plainly as the capability. Gecko composes payment
    rails and never becomes one, so an agent looking for custody or settlement
    should leave rather than try — a wrong tool confidently used is the failure
    this whole surface exists to prevent.

    Gate-filtered names come from the caller: one withholding rule, five doors.
    """
    # Deferred import keeps the routes single-sourced without an import cycle,
    # the same way build_onboard_breadcrumb does it.
    from .http_server import COMPREHEND_PATH, MCP_PATH

    base = public_url.rstrip("/") if public_url else ""

    def link(path: str) -> str:
        return f"{base}{path}" if base else path

    lines = [
        "# Gecko",
        "",
        "> Comprehended API surfaces served agent-native over Streamable HTTP:",
        "> first-call-correct tools, auth injected server-side, refusals that say why.",
        "",
        "## When to use this",
        "",
        "- You need to call an API you have never called before, correctly on the first",
        "  try, without reading its docs yourself.",
        "- You want the accounts, arguments and cost of a call checked BEFORE it is",
        "  signed or spent, not after.",
        "- You want to make your own API agent-usable: POST an OpenAPI URL to",
        f"  [{link(COMPREHEND_PATH)}]({link(COMPREHEND_PATH)}) and get served tools back.",
        "",
        "## When not to",
        "",
        "- Custody, settlement, or holding a key. Gecko composes payment rails and is",
        "  not one: it holds no funds, never signs a transaction, and takes no cut.",
        "  It DOES inject the API credential at call time, server-side, so the",
        "  credential never reaches the agent. Transaction signing belongs to your",
        "  own wallet or signer.",
        "- Discovering third-party APIs to buy. This host serves the surfaces its",
        "  operator chose to serve; it is not a marketplace.",
        "",
        "## Surfaces",
        "",
    ]
    if surface_names:
        for name in surface_names:
            url = link(f"/{name}{MCP_PATH}")
            lines.append(f"- [{name}]({url}): Streamable-HTTP MCP endpoint.")
            for query in _REPRESENTATIVE_QUERIES.get(name, _GENERIC_QUERIES):
                lines.append(f"  - ask it: {query}")
    else:
        lines.append("- No public surfaces are served on this host right now.")
    lines += [
        "",
        "## Discovery",
        "",
        f"- [MCP server card]({link('/.well-known/mcp/server-card.json')}): name, version,"
        " protocol and tools, readable before opening a session.",
        f"- [ARD catalog]({link('/.well-known/ard.json')}): what is available for a task"
        f" (also served at {link('/.well-known/ai-catalog.json')}).",
        f"- [Host manifest]({link('/.well-known/gecko.json')}): the surfaces and the"
        " submit door.",
        f"- [Onboarding]({link('/.well-known/onboard.md')}): how to use a surface, or"
        " onboard your own.",
        "",
        "## Docs",
        "",
        f"- [Quickstart]({_DOCS_QUICKSTART})",
        f"- [For providers]({_DOCS_FOR_PROVIDERS})",
        "",
    ]
    return "\n".join(lines)


def build_protected_resource_metadata(
    public_url: str | None, gated_surfaces: list[str]
) -> dict[str, Any]:
    """RFC 9728 protected-resource metadata for THIS host, kept honest.

    ``scopes_supported`` are the REAL permission scopes: per-surface grants,
    deny-by-default — a Gecko key opens exactly the surfaces its account was granted,
    spelled ``surface:<name>``. No ``authorization_servers`` member: no OAuth
    authorization server exists, and RFC 9728 makes the member optional — fabricating
    one is the wrong-but-well-formed breadcrumb this repo scores other surfaces down
    for. The WorkOS ``agent_auth`` block carries the self-serve mint path instead
    (identity = start the email login, claim = verify the code into a key)."""
    base = public_url.rstrip("/") if public_url else ""
    return {
        "resource": base or "/",
        "resource_name": "Gecko hosted MCP",
        "resource_documentation": "https://geckovision.tech/auth.md",
        "bearer_methods_supported": ["header"],
        "scopes_supported": sorted(f"surface:{name}" for name in gated_surfaces),
        "agent_auth": {
            "skill": "https://geckovision.tech/auth.md",
            "identity_types_supported": ["anonymous"],
            "identity_endpoint": f"{base}/auth/login/start"
            if base
            else "/auth/login/start",
            "claim_endpoint": f"{base}/auth/login/verify"
            if base
            else "/auth/login/verify",
        },
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
