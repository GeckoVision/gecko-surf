"""Below-scale surface sizing — the "don't truncate when you don't have to" rule.

Canon §6 (context-engineering reference): below ~200k tokens, skip retrieval and put the
whole surface in the prompt with caching. At single-API scale the lexical catalog
STRUCTURALLY cannot surface a zero-overlap paraphrase op — when any op genuinely matches,
``Catalog.search_scored`` returns only the score>0 matches and drops every score-0 op, so
bumping ``limit`` never recovers a paraphrase the query shares no token with. Truncating a
small surface therefore makes Gecko WORSE than the raw OpenAPI dump on first-call-correct
(the FCC eval: GECKO 1.00 -> 0.70 on clean, small Pegana; "dump all 41" kept 1.00).

Fix: when the usable surface is small enough to show in full, expose EVERY usable tool (no
top-k truncation) — so Gecko is strictly >= the raw dump. Only above the threshold does
top-k retrieval earn its truncation. This module is the single source of truth for that
threshold; the decision lives behind ``AgentApiClient.search`` (the agent-facing path),
while ``search_scored`` stays the pure ranked substrate the retrieval eval measures.
"""

from __future__ import annotations

import json
from typing import Any

# Practical op-count trigger. Below this many usable tool defs the full surface is cheap to
# show and STRICTLY dominates a lossy top-k (== the raw-dump baseline). Both committed pools
# sit below (txodds 18, pegana 26 usable); the real ~97-op TxODDS spec sits above and still
# retrieves. ~50 is where an always-visible tool list starts to compete for the agent's
# attention and retrieval begins to pay for its truncation.
SURFACE_ALL_MAX_OPS: int = 50

# Canon §6 hard ceiling (~200k tokens): even at a low op count, a spec with enormous
# inputSchemas that would blow the surface past this budget falls back to top-k. At <=50 ops
# with normal schemas the surface is ~30k tokens and this never fires — it is the principled
# backstop that keeps the op-count trigger aligned with the token budget, and the binding
# constraint only for pathological giant-schema APIs.
SURFACE_ALL_MAX_TOKENS: int = 200_000

# Chars-per-token heuristic (English prose + JSON ≈ 4 chars/token). Deliberately rough: we
# only need to know which SIDE of the 200k ceiling a ~30k surface is on, not an exact count.
_CHARS_PER_TOKEN: int = 4


def estimate_tool_tokens(tool: dict[str, Any]) -> int:
    """Rough token estimate for one agent-facing tool def (name + description + inputSchema)."""
    text = str(tool.get("name", "")) + str(tool.get("description", ""))
    schema = tool.get("inputSchema")
    if schema is not None:
        text += json.dumps(schema, separators=(",", ":"))
    return max(1, len(text) // _CHARS_PER_TOKEN)


def should_surface_all(usable_tools: list[dict[str, Any]]) -> bool:
    """True when the usable surface is small enough to show in full (no top-k truncation).

    Below BOTH the op-count trigger AND the token ceiling, the agent sees every usable tool —
    so Gecko is never worse than the raw OpenAPI dump on a small/clean API. Above either
    bound, top-k retrieval stays on. ``usable_tools`` are the auth-filtered tool defs (i.e.
    ``AgentApiClient.list_tools()``), so auth-gated ops a session can't satisfy never count
    toward the budget.
    """
    if len(usable_tools) > SURFACE_ALL_MAX_OPS:
        return False
    total = sum(estimate_tool_tokens(t) for t in usable_tools)
    return total <= SURFACE_ALL_MAX_TOKENS
