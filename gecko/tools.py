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


def _input_schema(op: Operation) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in _agent_params(op):
        schema = dict(p.schema) if isinstance(p.schema, dict) else {}
        if p.description and "description" not in schema:
            schema["description"] = p.description
        props[p.name] = schema
        if p.required:
            required.append(p.name)
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


def to_tool(op: Operation) -> dict[str, Any]:
    return {
        "name": _safe_name(op.operation_id),
        "description": question_description(op),
        "inputSchema": _input_schema(op),
        # Comprehension metadata: whether this op is auth-gated and which schemes.
        # The client uses this to hide ops a no-auth session can't satisfy.
        "requires_auth": _security_requires_auth(op),
        "auth_schemes": _auth_schemes(op),
        "_invoke": {
            "method": op.method,
            "path": op.path,
            "param_locations": {p.name: p.location for p in _agent_params(op)},
        },
    }


def build_tools(operations: list[Operation]) -> list[dict[str, Any]]:
    return [to_tool(o) for o in operations]
