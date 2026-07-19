"""Schema -> example generator for recorded mode.

The TxODDS spec ships almost no response examples, so to demo (and to validate)
without live calls we synthesize a minimal valid instance from each response
schema. Deterministic by design — same schema always yields the same sample.

Also home to the response-schema PICKERS (``success_schema`` / ``error_schema``):
which declared schema to synthesize FROM lives with the synthesizer, so both the
client (recorded mode) and the sandbox (probe mode) import them from here instead
of from each other — that shared need used to force a client<->sandbox import
cycle worked around with a lazy import.
"""

from __future__ import annotations

from typing import Any

_MAX_DEPTH = 8


def response_schema(op: Any, codes: tuple[str, ...]) -> dict[str, Any]:
    """The first declared JSON response schema among ``codes`` (in order)."""
    for code in codes:
        r = op.responses.get(code)
        if not isinstance(r, dict):
            continue
        content = r.get("content", {}) or {}
        media = content.get("application/json") or next(iter(content.values()), None)
        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
            return media["schema"]
    return {}


def success_schema(op: Any) -> dict[str, Any]:
    """The op's declared success-response schema — powers recorded/probe synthesis."""
    return response_schema(op, ("200", "201", "default"))


def error_schema(op: Any) -> dict[str, Any]:
    """The op's OWN declared error-response schema (sibling of ``success_schema``).

    The comprehension-native differentiator for probe mode: a malformed offline call
    answers with a body shaped like THIS API's error, not a generic Gecko message.
    ``422`` is scanned first so the body shape aligns with the synthetic 422 status
    the sandbox returns; then the other validation-adjacent codes, then ``default``.
    """
    return response_schema(op, ("422", "400", "409", "default"))


def example_from_schema(schema: Any, _depth: int = 0) -> Any:
    if not isinstance(schema, dict) or _depth > _MAX_DEPTH:
        return None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            return example_from_schema(schema[key][0], _depth + 1)
    if schema.get("allOf"):
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            val = example_from_schema(sub, _depth + 1)
            if isinstance(val, dict):
                merged.update(val)
        return merged or None

    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)

    if t == "object" or "properties" in schema or schema.get("required"):
        props = schema.get("properties", {}) or {}
        obj = {k: example_from_schema(v, _depth + 1) for k, v in props.items()}
        # Guarantee every REQUIRED field is present, even when the spec lists it in
        # `required` without a matching `properties` entry (common in large specs like
        # Stripe). Otherwise a nested required field is "missing" on an otherwise
        # well-formed synthesized call, failing recorded first-call-correctness.
        for req_key in schema.get("required") or []:
            if req_key not in obj:
                obj[req_key] = example_from_schema(props.get(req_key, {}), _depth + 1)
        return obj
    if t == "array":
        items = schema.get("items")
        return [example_from_schema(items, _depth + 1)] if items else []
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return False
    if t == "string":
        return (
            "2026-06-26T00:00:00Z" if schema.get("format") == "date-time" else "sample"
        )
    return None
