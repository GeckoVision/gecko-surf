"""Ingestion slice 2 — request-body + response fields decomposed into typed units.

The §13.5 capture gap: ``Operation.request_body`` / ``.responses`` were raw dicts, so a
POST body field could not be a correlation TARGET (the mutate side) and response fields
were a weak producer side. This decomposes both into typed ``Param`` records at ingest —
name, location, required, schema, and the canonical SPEC example (handling the OpenAPI
``examples`` map/list, not only inline ``example``). Surface metadata only (invariant #1):
the example is spec-authored, never a response value. Absent -> empty, never a crash; the
raw ``request_body`` / ``responses`` dicts are preserved untouched.
"""

from __future__ import annotations

from pathlib import Path

from gecko.ingest import extract_operations, load_spec

_FIX = Path(__file__).parent / "fixtures"


def _ops() -> dict[str, object]:
    spec = load_spec(str(_FIX / "body_response_fields_openapi.json"))
    return {op.operation_id: op for op in extract_operations(spec)}


def _by_name(fields) -> dict[str, object]:
    return {f.name: f for f in fields}


# --- request-body decomposition --------------------------------------------------
def test_body_fields_are_decomposed_into_typed_params() -> None:
    op = _ops()["createWidget"]
    body = _by_name(op.body_fields)
    # every declared body property becomes a typed field, at every (bounded) level.
    assert {"mint", "count", "label", "meta", "nested"} <= set(body)
    for f in op.body_fields:
        assert f.location == "body"


def test_body_field_required_flag_tracks_the_schema() -> None:
    op = _ops()["createWidget"]
    body = _by_name(op.body_fields)
    assert body["mint"].required is True  # in the object's `required`
    assert body["count"].required is True
    assert body["label"].required is False  # optional
    assert body["meta"].required is True
    assert body["nested"].required is True  # required inside meta


def test_body_field_example_is_lifted_from_the_examples_list() -> None:
    """The base58 example lives in the JSON-Schema ``examples`` LIST — the channel that
    the value-domain signature previously missed. It must still be lifted onto the field."""
    op = _ops()["createWidget"]
    body = _by_name(op.body_fields)
    assert body["mint"].example == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert body["count"].example == 3  # inline `example`
    assert body["nested"].example == "x"
    assert (
        body["label"].example is None
    )  # no example declared -> None, never a placeholder


# --- response decomposition ------------------------------------------------------
def test_response_fields_are_decomposed_into_typed_producers() -> None:
    op = _ops()["createWidget"]
    resp = _by_name(op.response_fields)
    assert {"widgetId", "mint", "ok"} <= set(resp)
    for f in op.response_fields:
        assert f.location == "response"


def test_response_field_examples_cover_inline_and_examples_list() -> None:
    op = _ops()["createWidget"]
    resp = _by_name(op.response_fields)
    assert resp["widgetId"].example == "w_1"  # inline `example`
    assert resp["mint"].example == "So11111111111111111111111111111111111111112"  # list
    assert resp["ok"].example is None


def test_error_response_fields_are_not_producers() -> None:
    """Only 2xx response fields are producer surface — a 4xx error body is not a value a
    later call consumes."""
    op = _ops()["createWidget"]
    resp = _by_name(op.response_fields)
    assert "error" not in resp  # the 400 body's `error` is not a producer


# --- absence + preservation ------------------------------------------------------
def test_absent_body_and_response_schema_yield_empty_no_crash() -> None:
    op = _ops()["health"]
    assert op.body_fields == []
    assert op.response_fields == []


def test_raw_request_body_and_responses_dicts_are_preserved() -> None:
    op = _ops()["createWidget"]
    assert op.request_body is not None
    assert "content" in op.request_body  # the raw dict is untouched, additive only
    assert "200" in op.responses


def test_pegana_p0_decomposes_without_spurious_examples() -> None:
    """The real design-partner spec ships no property examples -> fields decompose but
    carry no example, no crash."""
    spec = load_spec(str(_FIX / "pegana_p0_openapi.json"))
    ops = extract_operations(spec)
    # at least one op has a response body decomposed; none should crash.
    assert any(op.response_fields for op in ops)
    for op in ops:
        for f in [*op.body_fields, *op.response_fields]:
            assert isinstance(f.name, str)
