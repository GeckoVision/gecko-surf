"""Retrieval returns a SCOPE, not a surface — the attention budget of one search.

``surface_all`` (``gecko.scale``) is an ENUMERATION rule: below scale ``list_tools``
shows every usable tool in full, so Gecko is never worse than the raw OpenAPI dump and
the agent can always SEE every capability. That rule had leaked into RETRIEVAL — the MCP
``search_capabilities`` ranked through the same surface-all branch and then enriched
every hit with its full ``inputSchema``, so each search re-emitted the whole surface the
agent had already received at connect (measured: 43-op Pegana P0, five searches 91,089 B
against a 17,766 B connect — 5.1x the connect cost, entirely duplicate).

The split this module implements:

* **BREADTH is enumeration.** ``list_tools`` keeps every usable capability visible.
  Recall lives there and is untouched, at either scale.
* **DEPTH + ORDER is retrieval.** One search answers one intent: the ordered supplier
  ``plan``, and full schemas for exactly the ops that plan names. With no plan it is the
  ranked top-k, capped. Never the whole surface, at any scale.

Pure projection over already-computed comprehension state — no I/O, no auth, no client
back-reference — so it is falsifiable offline and can never re-import ``client``.
Control-plane clean by construction: it copies the search-hit shape plus ``inputSchema``
and never the ``_invoke`` wire-routing block.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: How many capabilities one search may return IN FULL when there is no plan to scope it.
#: Matches ``AgentApiClient.search``'s default ``limit`` so the ranked arm is the same
#: depth at either scale — the cap is now applied below scale too, which is the fix.
RETRIEVAL_MAX_TOOLS: int = 5

#: The per-tool projection retrieval hands back. Deliberately the frozen search-hit shape
#: (``name/summary/path/method``) plus the callable schema — additive, so a reader of the
#: old enriched hit sees no field disappear.
_HIT_FIELDS = ("name", "summary", "path", "method")


def _first_line(text: str) -> str:
    """A tool description's summary line — what a hit's ``summary`` carries."""
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def scope_item(
    name: str,
    hit: Mapping[str, Any] | None,
    tool: Mapping[str, Any] | None,
    *,
    method: str = "",
    path: str = "",
) -> dict[str, Any]:
    """One capability in a scope: the hit fields + the full ``inputSchema``.

    ``hit`` is the ranked search hit when retrieval produced one; a plan step can name an
    op that ranking never surfaced (above scale), in which case ``method``/``path`` come
    from the plan step itself and the summary from the tool def. ``_invoke`` is never
    copied — retrieval is control plane.
    """
    item: dict[str, Any] = {}
    if hit is not None:
        item.update({k: hit[k] for k in _HIT_FIELDS if k in hit})
    item.setdefault("name", name)
    if tool is not None:
        item.setdefault("summary", _first_line(tool.get("description", "")))
        item["inputSchema"] = tool["inputSchema"]
    item.setdefault("summary", "")
    item.setdefault("method", method)
    item.setdefault("path", path)
    return item


def plan_step_names(plan: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The plan's ops as ``(tool_name, method, path)`` in DERIVATION ORDER (goal last).

    A step's ``operation_id`` is the raw spec id; the agent-facing tool name is its
    sanitized form (``tools.safe_tool_name`` — the single source of truth both the tool
    defs and the catalog already agree on), so a spec with odd operationIds still scopes.
    """
    from .tools import safe_tool_name

    out: list[tuple[str, str, str]] = []
    for step in plan.get("steps", []) or []:
        if not isinstance(step, Mapping):
            continue
        op_id = str(step.get("operation_id", ""))
        if not op_id:
            continue
        out.append(
            (
                safe_tool_name(op_id),
                str(step.get("method", "")),
                str(step.get("path", "")),
            )
        )
    return out


def build_scope(
    hits: Sequence[Mapping[str, Any]],
    tools_by_name: Mapping[str, Mapping[str, Any]],
    plan: Mapping[str, Any] | None,
    *,
    limit: int = RETRIEVAL_MAX_TOOLS,
) -> dict[str, Any]:
    """Project one search into ``{"plan": ..., "tools": [...]}``.

    With a ``plan``, ``tools`` is exactly the ops the plan names, in derivation order —
    strictly smaller than the ranked list AND strictly more informative, because it
    carries the ordering and the join a flat list cannot. The steps are the scope, so a
    3-step chain returns 3 tools even when ``limit`` is smaller: the agent needs every
    link to execute the chain it was just handed.

    With no plan, ``tools`` is the ranked hits truncated to ``limit`` — the same top-k
    retrieval has always applied ABOVE scale, now applied below it too. An unknown name
    (a duck-typed client whose ranker and tool list disagree) still yields a hit entry,
    just without a schema, exactly as the pre-scope enrichment did.
    """
    hit_by_name = {str(h.get("name", "")): h for h in hits}
    items: list[dict[str, Any]] = []
    if plan is not None:
        seen: set[str] = set()
        for name, method, path in plan_step_names(plan):
            if name in seen:
                continue
            seen.add(name)
            items.append(
                scope_item(
                    name,
                    hit_by_name.get(name),
                    tools_by_name.get(name),
                    method=method,
                    path=path,
                )
            )
    if not items:
        for hit in hits[:limit]:
            name = str(hit.get("name", ""))
            items.append(scope_item(name, hit, tools_by_name.get(name)))
    return {"plan": dict(plan) if plan is not None else None, "tools": items}


__all__ = ["RETRIEVAL_MAX_TOOLS", "build_scope", "plan_step_names", "scope_item"]
