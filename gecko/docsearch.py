"""Virtualized-docs search — the self-heal ``query_docs`` engine.

An agent that mis-calls an API needs to learn WHY offline and rewrite, without
re-reading human docs or firing another failing call. ``search_docs`` answers a
plain-language intent from the comprehended surface's own CONTROL-PLANE artifacts:
the catalog's lexical hits, each matched op's spec-derived summary + params, its
callable ``inputSchema``, and (best-effort) the agent-native artifacts
(tools.md / llms.txt). The "filesystem" in the founder's ``query_docs_filesystem``
name is a METAPHOR — nothing here mounts a real filesystem; it is a search over
spec-derived artifacts.

Control plane (invariant #1 / #4): every field returned is derived from the spec —
never an auth header, never the tool's private ``_invoke`` routing, never a request
payload or arg value. We hand-pick ``{name, summary, path, method, description,
params, inputSchema}`` from each tool def rather than echoing the whole def, so the
auth-hiding guarantee the tool layer already provides is preserved by construction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import AgentApiClient

#: Cap on matched tools + artifact snippets so a huge surface can't blow the budget.
_DEFAULT_LIMIT = 3


def _descriptor(
    pname: str, pspec: dict[str, Any], required: set[str]
) -> dict[str, Any]:
    """One spec-derived param row — name, type, required flag, and the param's own
    description. No defaults, no examples, no arg values."""
    return {
        "name": pname,
        "type": pspec.get("type", "any"),
        "required": pname in required,
        "description": pspec.get("description", ""),
    }


def _params_from_schema(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Spec-derived param descriptors from a tool's ``inputSchema``.

    A request-body op wraps its fields under a single ``body`` object; flatten that one
    level (the same depth the sandbox validates) so the agent sees the real fields —
    ``amount``, ``to`` — instead of an opaque ``body``. Any sibling path/query params
    are kept alongside."""
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    params: list[dict[str, Any]] = []
    for pname, pspec in props.items():
        spec = pspec or {}
        nested = spec.get("properties")
        if pname == "body" and isinstance(nested, dict):
            body_required = set(spec.get("required", []) or [])
            for bname, bspec in nested.items():
                params.append(_descriptor(bname, bspec or {}, body_required))
        else:
            params.append(_descriptor(pname, spec, required))
    return params


def _artifact_snippets(client: AgentApiClient, matched_names: set[str]) -> list[str]:
    """Best-effort agent-native doc snippets for the matched tools, pulled from the
    already-control-plane-safe ``tools.md`` (every field routed through the artifact
    sanitizer). A failure here is non-fatal — self-heal still has the catalog hits."""
    try:
        from .agentnative import build_artifacts

        tools_md = build_artifacts(client).get("tools.md", "")
    except Exception:  # noqa: BLE001 - artifacts are additive; never break query_docs
        return []
    snippets: list[str] = []
    # tools.md sections are delimited by a ``## <tool name>`` heading; keep the ones
    # whose heading matches a tool the intent surfaced.
    for section in tools_md.split("\n## ")[1:]:
        head = section.splitlines()[0].strip()
        if head in matched_names:
            snippets.append("## " + section.strip())
    return snippets


def search_docs(
    client: AgentApiClient, intent: str, *, limit: int = _DEFAULT_LIMIT
) -> dict[str, Any]:
    """Search the comprehended surface's virtualized docs for ``intent``.

    Returns ``{intent, matches, docs}`` where each match carries the op's spec-derived
    summary/description/params and its callable ``inputSchema`` (so the agent sees what
    the call expects), and ``docs`` holds the matching agent-native artifact snippets.
    Control-plane only — see the module docstring for the guarantee.
    """
    from .client import ToolNotFound

    hits = client.search(intent, limit=limit)
    matches: list[dict[str, Any]] = []
    matched_names: set[str] = set()
    for hit in hits:
        name = hit["name"]
        try:
            tool = client.get_tool(name)
        except ToolNotFound:
            continue  # a hit the session can't actually use — don't advertise it
        schema = tool.get("inputSchema", {}) or {}
        matched_names.add(name)
        matches.append(
            {
                "name": name,
                "summary": hit["summary"],
                "path": hit["path"],
                "method": hit["method"],
                "description": tool.get("description", ""),
                "params": _params_from_schema(schema),
                # The callable param schema — hides auth headers (invariant #4) and
                # carries no ``_invoke`` routing; it is the "how to fix the call" contract.
                "inputSchema": schema,
            }
        )
    return {
        "intent": intent,
        "matches": matches,
        "docs": _artifact_snippets(client, matched_names),
    }
