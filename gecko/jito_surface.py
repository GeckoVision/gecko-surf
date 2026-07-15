"""The Jito Block Engine money-boundary — the ONE place both hosts build its surface.

Jito's Block Engine is mainnet-money infrastructure. Its six comprehended ops split on a
founder-confirmed line — *we are the catalog, not the relay*:

  * READ ops -> served LIVE (public, keyless, no signing, no spend): ``getTipFloor``,
    ``getTipAccounts``, ``getBundleStatuses``, ``getInflightBundleStatuses``. An agent
    gets real tip-floor / bundle-status data straight from mainnet.
  * WRITE ops -> kept RECORDED (money-movers we CATALOG but must NEVER relay):
    ``sendBundle``, ``sendTransaction``. Serving these live would turn our public endpoint
    into an open MEV relay (a control-plane violation). The agent takes Gecko's
    first-call-correct comprehension and submits these DIRECTLY to Jito with its OWN
    wallet — our server is the catalog/broadcaster's map, never the broadcaster.

This lives in ONE module so the two hosts that serve Jito (``serve_mcp``,
``serve_providers``) enforce the identical boundary — a money-boundary must not be able to
drift between hosts. The recorded/live split is realized via ``McpSurface.recorded_ops``
(per-op override): the write ops stay recorded even though the surface mode is live, so
their response is synthesized from the schema and never reaches the wire.
"""

from __future__ import annotations

from pathlib import Path

from .access import public_session
from .client import AgentApiClient
from .enforce import EnforceMode
from .mcp_server import McpSurface
from .tools import tool_name

# In the image: /app/gecko/jito_surface.py -> parents[1] = /app (repo root); matches
# serve_mcp/serve_providers so the shipped spec resolves identically on every host.
_ROOT = Path(__file__).resolve().parents[1]
JITO_SPEC_PATH = _ROOT / "examples" / "jito" / "spec" / "jito_blockengine_openapi.json"

# The jito spec declares TWO servers (mainnet first, testnet second); live mode fails
# closed on an ambiguous multi-server spec (client.AmbiguousServerError) unless the target
# is pinned. We pin mainnet — the host this surface has always de-facto served. Keyless
# public JSON-RPC reads; ``public_session`` means no secret exists to inject toward the pin.
JITO_MAINNET_BASE = "https://mainnet.block-engine.jito.wtf"

# The two money-moving WRITE ops, BY operationId (== the JSON-RPC method). Kept RECORDED
# even on the live surface: catalog, never relay. Resolved to agent-facing tool names via
# the SAME derivation the surface uses (``tools.tool_name``), so this stays correct even if
# an operationId gets sanitized.
JITO_WRITE_OP_IDS: tuple[str, ...] = ("sendBundle", "sendTransaction")


class JitoBoundaryError(Exception):
    """A money-moving write op could not be resolved to a tool name — fail CLOSED.

    Refusing to build the live surface beats risking an unpinned money-mover on the wire:
    if the spec drifts so ``sendBundle``/``sendTransaction`` no longer resolve, we raise
    rather than silently serve them live.
    """


def resolve_write_tool_names(client: AgentApiClient) -> frozenset[str]:
    """Resolve ``JITO_WRITE_OP_IDS`` to agent-facing tool names via ``tools.tool_name``.

    Fails closed (raises ``JitoBoundaryError``) if any money-mover is missing from the
    comprehended spec — a write we can't pin must never be waved through to the live path.
    """
    by_id = {op.operation_id: op for op in client.operations}
    missing = [oid for oid in JITO_WRITE_OP_IDS if oid not in by_id]
    if missing:
        raise JitoBoundaryError(
            f"jito money-moving write op(s) not found in spec: {sorted(missing)}"
        )
    return frozenset(tool_name(by_id[oid]) for oid in JITO_WRITE_OP_IDS)


def build_jito_surface(enforce: EnforceMode | None) -> McpSurface:
    """Build the hosted Jito surface: reads LIVE against mainnet, the two money-moving
    writes RECORDED (catalog-only). Both hosts that serve Jito build it HERE so the split
    can't drift. ``enforce`` sets the risk-gate stance (hosted callers pass ``block``)."""
    client = AgentApiClient(
        str(JITO_SPEC_PATH),
        base_url=JITO_MAINNET_BASE,
        session=public_session(),
    )
    return McpSurface(
        client,
        mode="live",
        enforce=enforce,
        recorded_ops=resolve_write_tool_names(client),
    )
