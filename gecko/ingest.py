"""OpenAPI surface ingestor.

Parses an OpenAPI 3.x document (YAML or JSON) into a normalized list of
``Operation`` records with local ``$ref``s resolved. This is the *surface only*
(method, path, params, request/response schemas) — never response data.

Stdlib + PyYAML only, by design: the ingestor must run anywhere with zero heavy
deps, so it can be lifted into the eventual product repo unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from .netguard import safe_get

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
_MAX_REF_DEPTH = 12  # bound recursion on self-referential schemas


@dataclass
class Param:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    schema: dict[str, Any]
    description: str = ""


@dataclass
class Operation:
    method: str  # uppercase
    path: str
    operation_id: str
    summary: str
    description: str
    tags: list[str]
    parameters: list[Param]
    request_body: dict[str, Any] | None
    responses: dict[str, Any]
    security: list[Any] = field(default_factory=list)


def load_spec(src: str) -> dict[str, Any]:
    """Load an OpenAPI doc from a local path or http(s) URL.

    ``yaml.safe_load`` parses JSON too, so this handles both ``.yaml`` and
    ``.json`` specs.
    """
    if src.startswith(("http://", "https://")):
        # SSRF guard: validate + cap redirects/size/timeout before/while fetching an
        # untrusted spec URL (never trust the bytes; never let it reach a private host).
        raw = safe_get(src)
    else:
        with open(src, encoding="utf-8") as fh:
            raw = fh.read()
    spec = yaml.safe_load(raw)
    if not isinstance(spec, dict):
        raise ValueError("OpenAPI document did not parse to a mapping")
    return spec


def _lookup(spec: dict[str, Any], ref: str) -> Any:
    """Resolve a local JSON-pointer ``$ref`` (e.g. '#/components/schemas/Foo')."""
    if not ref.startswith("#/"):
        return None
    cur: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")  # JSON-pointer unescape
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def resolve_refs(
    node: Any,
    spec: dict[str, Any],
    _depth: int = 0,
    _seen: frozenset[str] = frozenset(),
) -> Any:
    """Recursively dereference local ``$ref``s, with cycle + depth guards.

    On a cycle or when the depth cap is hit, the ``$ref`` is left in place rather
    than expanded — callers still get a usable (if shallow) schema.
    """
    if _depth > _MAX_REF_DEPTH:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _seen:
                return {"$ref": ref}
            target = _lookup(spec, ref)
            if target is None:
                return node
            return resolve_refs(target, spec, _depth + 1, _seen | {ref})
        return {k: resolve_refs(v, spec, _depth + 1, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_refs(v, spec, _depth + 1, _seen) for v in node]
    return node


def extract_operations(spec: dict[str, Any]) -> list[Operation]:
    """Flatten ``paths`` into a normalized list of operations with refs resolved.

    Path-level parameters are merged into each operation's own parameters.
    """
    operations: list[Operation] = []
    global_security = spec.get("security", []) or []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters", []) or []
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            params: list[Param] = []
            for raw in [*shared_params, *(op.get("parameters") or [])]:
                p = resolve_refs(raw, spec)
                if not isinstance(p, dict):
                    continue
                location = p.get("in", "")
                params.append(
                    Param(
                        name=p.get("name", ""),
                        location=location,
                        required=bool(p.get("required", location == "path")),
                        schema=resolve_refs(p.get("schema", {}), spec),
                        description=p.get("description", ""),
                    )
                )
            request_body = op.get("requestBody")
            if request_body is not None:
                request_body = resolve_refs(request_body, spec)
            operations.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    operation_id=op.get("operationId") or f"{method.lower()}_{path}",
                    summary=op.get("summary", ""),
                    description=op.get("description", ""),
                    tags=list(op.get("tags", []) or []),
                    parameters=params,
                    request_body=request_body,
                    responses=resolve_refs(op.get("responses", {}) or {}, spec),
                    security=op.get("security", global_security),
                )
            )
    return operations
