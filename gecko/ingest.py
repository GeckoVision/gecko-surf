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
_MAX_EXAMPLE_DEPTH = 6  # bound the schema-property walk for canonical examples


@dataclass
class Param:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    schema: dict[str, Any]
    description: str = ""
    # Canonical example lifted from the SPEC (the param's ``example``/``examples`` or
    # the param schema's ``example``/``examples``). Surface metadata, NEVER a response
    # payload (invariant #1). Double win: (a) a real value (e.g. a real mint) lets
    # verify-docs --live build an honest path/query call instead of a placeholder that
    # false-404s; (b) a declared example is value-domain / correlation evidence. None
    # when the spec ships no example for this param.
    example: Any = None


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
    # Canonical examples lifted from request/response schema PROPERTIES: ``{field ->
    # example value}``. Additive, control-plane (surface metadata from the spec, never
    # observed traffic). Correlation evidence: a field that ships a real example
    # self-describes its value domain. Empty when the spec declares no property example.
    field_examples: dict[str, Any] = field(default_factory=dict)


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


def _first_example(examples: Any) -> Any:
    """The first example VALUE from either OpenAPI shape, or None.

    Handles the OpenAPI *media/parameter* ``examples`` MAP (``{name: {value: ...}}``)
    and the JSON-Schema 3.1 ``examples`` LIST. Surface metadata only — the value is a
    spec-authored sample, never observed traffic.
    """
    if isinstance(examples, dict):
        for ex in examples.values():
            if isinstance(ex, dict) and "value" in ex:
                return ex["value"]
        return None
    if isinstance(examples, list) and examples:
        return examples[0]
    return None


def _schema_example(schema: Any) -> Any:
    """The canonical example a schema declares (``example`` then ``examples``), or None."""
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]
    return _first_example(schema.get("examples"))


def _param_example(param_obj: dict[str, Any], schema: Any) -> Any:
    """The canonical example for a parameter: prefer the parameter object's own
    ``example``/``examples``, then fall back to the schema's. None when absent."""
    if "example" in param_obj:
        return param_obj["example"]
    ex = _first_example(param_obj.get("examples"))
    if ex is not None:
        return ex
    return _schema_example(schema)


def _json_schemas(op: dict[str, Any]) -> list[dict[str, Any]]:
    """The request-body + response ``application/json`` schemas of a path-item op
    (already ref-resolved by the caller) — the surfaces we lift property examples from."""
    schemas: list[dict[str, Any]] = []

    def _from_content(container: Any) -> None:
        if not isinstance(container, dict):
            return
        content = container.get("content")
        if not isinstance(content, dict):
            return
        media = content.get("application/json") or next(iter(content.values()), None)
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            schemas.append(media["schema"])

    _from_content(op.get("requestBody"))
    for resp in (op.get("responses") or {}).values():
        _from_content(resp)
    return schemas


def _collect_field_examples(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk schema properties (bounded/cycle-guarded) collecting ``{field -> example}``
    for every property that declares a canonical example. First example per name wins
    (deterministic document order). Surface metadata only — never a response value."""
    out: dict[str, Any] = {}

    def walk(node: Any, depth: int, seen: frozenset[int]) -> None:
        if depth > _MAX_EXAMPLE_DEPTH or not isinstance(node, dict) or id(node) in seen:
            return
        seen = seen | {id(node)}
        for name, sub in (node.get("properties") or {}).items():
            if not isinstance(sub, dict):
                continue
            ex = _schema_example(sub)
            if ex is not None and name not in out:
                out[name] = ex
            walk(sub, depth + 1, seen)
        walk(node.get("items") or {}, depth + 1, seen)
        for key in ("allOf", "oneOf", "anyOf"):
            for sub in node.get(key) or []:
                walk(sub, depth + 1, seen)

    for schema in schemas:
        walk(schema, 0, frozenset())
    return out


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
                schema = resolve_refs(p.get("schema", {}), spec)
                params.append(
                    Param(
                        name=p.get("name", ""),
                        location=location,
                        required=bool(p.get("required", location == "path")),
                        schema=schema,
                        description=p.get("description", ""),
                        example=_param_example(p, schema),
                    )
                )
            request_body = op.get("requestBody")
            if request_body is not None:
                request_body = resolve_refs(request_body, spec)
            responses = resolve_refs(op.get("responses", {}) or {}, spec)
            op_resolved = {"requestBody": request_body, "responses": responses}
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
                    responses=responses,
                    security=op.get("security", global_security),
                    field_examples=_collect_field_examples(_json_schemas(op_resolved)),
                )
            )
    return operations
