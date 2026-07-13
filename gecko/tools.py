"""Question-shaped tool generator — the comprehension payload.

Turns a normalized ``Operation`` into an agent-reasonable tool definition
(name + question-shaped description + JSON-Schema input), MCP-compatible.

Two comprehension decisions that separate this from a raw OpenAPI dump:
1. **Hide the plumbing.** Auth headers (Authorization / X-Api-Token) are removed
   from the agent-facing input — the access layer injects them. The agent only
   sees decision-relevant inputs.
2. **Carry invocation metadata** (`_invoke`: method, path, param locations) so a
   caller can build the real HTTP request without re-parsing the spec.
"""

from __future__ import annotations

import re
from typing import Any

from . import sanitize
from .ingest import Operation, Param

_AUTH_HEADERS = {"authorization", "x-api-token", "x-apikey", "api-key", "x-api-key"}


def _is_auth_param(p: Param) -> bool:
    return p.location == "header" and p.name.lower() in _AUTH_HEADERS


def _agent_params(op: Operation) -> list[Param]:
    """Params the agent should reason about — plumbing headers removed."""
    return [p for p in op.parameters if not _is_auth_param(p)]


def _body_schema(op: Operation) -> tuple[dict[str, Any] | None, bool]:
    rb = op.request_body
    if not isinstance(rb, dict):
        return None, False
    content = rb.get("content", {}) or {}
    media = content.get("application/json") or (next(iter(content.values()), None))
    schema = media.get("schema") if isinstance(media, dict) else None
    return (schema if isinstance(schema, dict) else None), bool(rb.get("required"))


def _safe_key(name: str) -> str:
    """An Anthropic/OpenAI tool-schema property key must match ``^[a-zA-Z0-9_.-]{1,64}$``.
    A param named ``filter[user]`` / ``page[size]`` (JSON:API — a common style) otherwise
    makes the whole tool def rejected (400), so the agent can't use it at all. Sanitize the
    agent-facing key; the caller maps it back to the real name via ``arg_aliases``."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:64] or "_"


def _agent_key_map(op: Operation) -> dict[str, str]:
    """Real param name -> Anthropic-safe schema key. Deterministic + collision-free, so the
    schema (keys/required) and the caller (arg_aliases) agree on the same sanitized keys."""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for p in _agent_params(op):
        safe = _safe_key(p.name)
        while safe != p.name and safe in used:  # dedupe a sanitize-induced collision
            safe = f"{safe}_"
        used.add(safe)
        mapping[p.name] = safe
    return mapping


def _input_schema(op: Operation) -> dict[str, Any]:
    keymap = _agent_key_map(op)
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in _agent_params(op):
        schema = dict(p.schema) if isinstance(p.schema, dict) else {}
        if p.description and "description" not in schema:
            schema["description"] = p.description
        key = keymap[p.name]
        props[key] = schema
        if p.required:
            required.append(key)
    body_schema, body_required = _body_schema(op)
    if body_schema is not None:
        props["body"] = body_schema
        if body_required:
            required.append("body")
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def question_description(op: Operation) -> str:
    parts = [op.summary or op.operation_id]
    agent_params = _agent_params(op)
    required = [p.name for p in agent_params if p.required]
    optional = [p.name for p in agent_params if not p.required]
    if required:
        parts.append("Required: " + ", ".join(required) + ".")
    if optional:
        parts.append("Optional: " + ", ".join(optional) + ".")
    return " ".join(parts)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]


def tool_name(op: Operation) -> str:
    """The single source of truth for an op's agent-facing tool name.

    Both ``to_tool`` (the tool def) and the catalog must agree on this, or
    ``client.search`` — which filters hits against sanitized tool names — drops
    every result for specs whose operationId is synthesized/contains odd chars.
    """
    return _safe_name(op.operation_id)


def _security_requires_auth(op: Operation) -> bool:
    """True only if *every* way to call this op needs auth.

    OpenAPI ``security`` is a list of alternative requirement objects; satisfying
    ANY one grants access. An empty ``{}`` requirement means "no auth is also
    acceptable", so its presence makes auth optional. An empty/absent list means
    no auth at all.
    """
    sec = op.security
    if not sec:
        return False
    return all(bool(req) for req in sec)


def _auth_schemes(op: Operation) -> list[str]:
    """The security-scheme names this op references (for diagnostics/comprehension)."""
    names: set[str] = set()
    for req in op.security or []:
        if isinstance(req, dict):
            names.update(req.keys())
    return sorted(names)


# Where a securityScheme places the secret. A header/http/bearer scheme is safe; a
# query/path/cookie placement would land the secret in a loggable URL — refuse to inject.
_LOGGABLE_AUTH_LOCATIONS = frozenset({"query", "path", "cookie"})


def auth_location_is_safe(spec: dict[str, Any], op: Operation) -> bool:
    """False if any securityScheme this op references drifts the secret out of a header
    and into a loggable place (query/path/cookie). http/bearer schemes have no ``in``
    and are header-shaped, hence safe. Pins the auth *location*, not just the host."""
    components = spec.get("components") if isinstance(spec, dict) else None
    schemes = (components or {}).get("securitySchemes", {}) or {}
    for name in _auth_schemes(op):
        scheme = schemes.get(name)
        if isinstance(scheme, dict):
            location = str(scheme.get("in", "")).lower()
            if location in _LOGGABLE_AUTH_LOCATIONS:
                return False
    return True


def to_tool(op: Operation) -> dict[str, Any]:
    # Anti-poisoning: the summary/description and every param schema come from the
    # (untrusted) spec. Neutralize any injected instruction / secret-looking default
    # before it reaches the agent, and flag the surface for quarantine if we had to.
    description, poisoned_desc = sanitize.sanitize_text(question_description(op))
    input_schema, poisoned_schema = sanitize.sanitize_schema(_input_schema(op))
    tool: dict[str, Any] = {
        "name": tool_name(op),
        "description": description,
        "inputSchema": input_schema,
        # Comprehension metadata: whether this op is auth-gated and which schemes.
        # The client uses this to hide ops a no-auth session can't satisfy.
        "requires_auth": _security_requires_auth(op),
        "auth_schemes": _auth_schemes(op),
        "_invoke": {
            "method": op.method,
            "path": op.path,
            "param_locations": {p.name: p.location for p in _agent_params(op)},
            # safe_key -> real param name, for params whose name had to be sanitized
            # (e.g. JSON:API `filter[user]`); the caller translates the agent's args back.
            "arg_aliases": {
                safe: real for real, safe in _agent_key_map(op).items() if safe != real
            },
        },
    }
    if poisoned_desc or poisoned_schema:
        # A poisoned op quarantines its whole surface (client enforces recorded-only,
        # no auth). Kept as spec metadata (a bool), never the stripped instruction text.
        tool["x-poison-flag"] = True
    return tool


def build_tools(operations: list[Operation]) -> list[dict[str, Any]]:
    return [to_tool(o) for o in operations]
