"""The Kora money-boundary — the ONE place every host builds its surface.

Kora is a Solana fee payer: it signs a transaction as the fee payer so the caller needs no
SOL, and charges for that in a token instead. It therefore HOLDS A FUNDED KEY, which makes
its surface the same shape as Jito's and one step closer to the money.

The split is the one ``jito_surface`` already established, and no new ruling is needed —
*we are the catalog, not the relay*:

  * READ ops -> served LIVE (no signing, no spend): ``getVersion``, ``liveness``,
    ``getBlockhash``, ``getSupportedTokens``, ``getPayerSigner``, ``getConfig`` and
    ``estimateTransactionFee``. The last one simulates to price a transaction and is the
    call that answers "what does this cost when the wallet holds no SOL".
  * WRITE ops -> kept RECORDED (money-movers we CATALOG but must NEVER relay):
    ``signTransaction``, ``signAndSendTransaction``, ``transferTransaction``. Every one
    spends the fee payer's SOL. Serving them live from a public mount would let any
    anonymous caller drain our fee payer, which is the control-plane violation the Jito
    module refuses for the same reason. The agent takes Gecko's first-call-correct
    comprehension and calls Kora DIRECTLY, with its own node and its own credentials.

WHY THE SPEC IS OURS AND SAYS SO. Kora publishes no usable specification: its own
generator emits OpenAPI 3.1.0 with thirty schemas and **zero operations**, because every
method is ``POST /`` carrying a JSON-RPC envelope and OpenAPI describes (path, verb) pairs.
``examples/kora_demo/spec/kora_openapi.json`` is therefore RECOVERED FROM SOURCE, and its
``info.description`` records that, the version it was read at, and the three facts that
were verified against a running node rather than inferred:

  1. the server accepts a per-method path — ``POST /getVersion`` answers exactly as
     ``POST /`` does — so per-method paths are WIRE-ACCURATE here, unlike the virtual
     ``/{method}`` spec the Jito module warns about;
  2. parameters are NAMED, and positional parameters are refused outright
     (``invalid type: map, expected a string``), which is the opposite of the convention
     most Solana JSON-RPC uses;
  3. a no-argument method takes an empty object.

None of the three appears in any Kora document. They are the difference between a call that
lands and a call that comes back as a parse error.
"""

from __future__ import annotations

from pathlib import Path

from .access import public_session
from .client import AgentApiClient
from .enforce import EnforceMode
from .mcp_server import McpSurface
from .tools import tool_name

# In the image: /app/gecko/kora_surface.py -> parents[1] = /app (repo root), matching
# serve_mcp so the shipped spec resolves identically on every host.
_ROOT = Path(__file__).resolve().parents[1]

KORA_SPEC_PATH = _ROOT / "examples" / "kora_demo" / "spec" / "kora_openapi.json"

#: The money-moving ops, BY operationId (== the JSON-RPC method). Kept RECORDED even on a
#: live surface: catalog, never relay. Resolved to agent-facing tool names through the SAME
#: derivation the surface uses, so this stays correct if an operationId is ever sanitized.
KORA_WRITE_OP_IDS: tuple[str, ...] = (
    "signTransaction",
    "signAndSendTransaction",
    "transferTransaction",
)


class KoraBoundaryError(Exception):
    """A money-moving op could not be resolved to a tool name — fail CLOSED.

    Refusing to build the surface beats risking an unpinned fee-payer spender on the wire.
    If the spec drifts so a write no longer resolves, we raise rather than serve it live.
    """


def resolve_write_tool_names(client: AgentApiClient) -> frozenset[str]:
    """Resolve ``KORA_WRITE_OP_IDS`` to agent-facing tool names, failing closed."""
    by_id = {op.operation_id: op for op in client.operations}
    missing = [oid for oid in KORA_WRITE_OP_IDS if oid not in by_id]
    if missing:
        raise KoraBoundaryError(
            f"kora money-moving op(s) not found in spec: {sorted(missing)}"
        )
    return frozenset(tool_name(by_id[oid]) for oid in KORA_WRITE_OP_IDS)


def build_kora_surface(base_url: str, enforce: EnforceMode | None) -> McpSurface:
    """Build the Kora surface: reads LIVE against ``base_url``, money-movers RECORDED.

    ``base_url`` is required and never defaulted. A fee payer's node is not a public good
    and there is no address that is right for everyone, so the operator must name the node
    it is willing to put behind this mount. The pin is also the trust anchor the auth guard
    rests on.
    """
    if not base_url:
        raise KoraBoundaryError(
            "kora surface needs an explicit base_url; there is no default node"
        )
    client = AgentApiClient(
        str(KORA_SPEC_PATH), base_url=base_url, session=public_session()
    )
    return McpSurface(
        client,
        mode="live",
        enforce=enforce,
        recorded_ops=resolve_write_tool_names(client),
    )


def build_kora_catalog_surface(enforce: EnforceMode | None) -> McpSurface:
    """Build Kora as a pure CATALOG: every op recorded, nothing reaches any node.

    This is what a public mount may serve. It answers "how do I call Kora correctly" for
    all ten methods, in full, at $0, while reaching no node and spending nobody's SOL. An
    operator who wants the live reads runs ``build_kora_surface`` against their own node on
    a gated mount.
    """
    client = AgentApiClient(
        str(KORA_SPEC_PATH),
        base_url="https://kora.invalid",
        session=public_session(),
    )
    return McpSurface(client, mode="recorded", enforce=enforce)
