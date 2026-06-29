"""Schema -> example generator for recorded mode.

The TxODDS spec ships almost no response examples, so to demo (and to validate)
without live calls we synthesize a minimal valid instance from each response
schema. Deterministic by design — same schema always yields the same sample.
"""

from __future__ import annotations

from typing import Any

_MAX_DEPTH = 8


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

    if t == "object" or "properties" in schema:
        props = schema.get("properties", {}) or {}
        return {k: example_from_schema(v, _depth + 1) for k, v in props.items()}
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
