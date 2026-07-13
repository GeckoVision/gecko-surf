"""Task 2.1 — ``_error_schema(op)``: the comprehension-native differentiator.

For an op whose spec declares an error response schema (422/400/409/default),
probe mode answers a malformed call with a body shaped like THAT API's error —
not a generic Gecko message. Mirrors ``_success_schema``.
"""

from __future__ import annotations

from typing import Any

from gecko.client import _error_schema, _success_schema
from gecko.ingest import extract_operations
from gecko.sample import example_from_schema


def _op_with_responses(responses: dict[str, Any]) -> Any:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "summary": "x",
                    "responses": responses,
                }
            }
        },
    }
    return extract_operations(spec)[0]


def _json_response(schema: dict[str, Any]) -> dict[str, Any]:
    return {"content": {"application/json": {"schema": schema}}}


ERROR_422 = {
    "type": "object",
    "properties": {"error_code": {"type": "string"}, "detail": {"type": "string"}},
    "required": ["error_code"],
}
ERROR_400 = {"type": "object", "properties": {"message": {"type": "string"}}}
DEFAULT_ERR = {"type": "object", "properties": {"fault": {"type": "string"}}}


def test_error_schema_returns_the_declared_422_schema() -> None:
    op = _op_with_responses({"422": _json_response(ERROR_422)})
    assert _error_schema(op) == ERROR_422


def test_error_schema_prefers_422_so_body_matches_the_synthetic_status() -> None:
    op = _op_with_responses(
        {"400": _json_response(ERROR_400), "422": _json_response(ERROR_422)}
    )
    assert _error_schema(op) == ERROR_422


def test_error_schema_falls_back_through_400_then_default() -> None:
    op = _op_with_responses({"400": _json_response(ERROR_400)})
    assert _error_schema(op) == ERROR_400
    op = _op_with_responses({"default": _json_response(DEFAULT_ERR)})
    assert _error_schema(op) == DEFAULT_ERR


def test_error_schema_empty_when_the_spec_declares_none() -> None:
    op = _op_with_responses({"200": _json_response({"type": "object"})})
    assert _error_schema(op) == {}


def test_error_example_is_shaped_like_that_apis_error() -> None:
    op = _op_with_responses({"422": _json_response(ERROR_422)})
    body = example_from_schema(_error_schema(op))
    assert isinstance(body, dict)
    assert body["error_code"] == "sample"
    assert "detail" in body


def test_success_schema_still_resolves_at_module_level() -> None:
    ok = {"type": "object", "properties": {"balance": {"type": "number"}}}
    op = _op_with_responses(
        {"200": _json_response(ok), "422": _json_response(ERROR_422)}
    )
    assert _success_schema(op) == ok
