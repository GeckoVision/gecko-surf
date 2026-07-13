"""Task 2.2 — ``gecko.sandbox.evaluate``: three gates -> a synthetic result.

A malformed probe call must come BACK as a result (the API's own error shape +
remediation), never a raised ``CallError`` — that is the self-heal loop's whole
input signal. Control-plane gate: the sandbox module can never write the corpus.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import gecko.sandbox as sandbox
from gecko.ingest import extract_operations
from gecko.sandbox import SimResult, evaluate

SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Pay API", "version": "1"},
    "paths": {
        "/balance": {
            "get": {
                "operationId": "getBalance",
                "summary": "Read the balance.",
                "parameters": [
                    {
                        "name": "account",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "currency",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "enum": ["USD", "EUR"]},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    },
                    "422": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error_code": {"type": "string"},
                                        "detail": {"type": "string"},
                                    },
                                    "required": ["error_code"],
                                }
                            }
                        }
                    },
                },
            }
        },
        "/withdraw": {
            "post": {
                "operationId": "createWithdraw",
                "summary": "Withdraw funds.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "amount": {"type": "number"},
                                    "to": {"type": "string"},
                                },
                                "required": ["amount"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    }
                },
            }
        },
    },
}

_OPS = {op.operation_id: op for op in extract_operations(SPEC)}


def test_missing_required_returns_a_synthetic_422_not_an_exception() -> None:
    result = evaluate(_OPS["getBalance"], {})
    assert isinstance(result, SimResult)
    assert result.status == 422
    assert result.mode == "probe"
    assert "schema.required" in result.signals
    # remediation is the generic fix-string map (code constants, no arg values)
    assert "schema.required" in result.remediation
    # error-shaped: synthesized from THIS API's declared 422 schema
    assert result.data["error_code"] == "sample"


def test_enum_violation_fails_the_schema_gate() -> None:
    result = evaluate(_OPS["getBalance"], {"account": "a", "currency": "GBP"})
    assert result.status == 422
    assert "schema.enum" in result.signals


def test_missing_body_required_field_is_caught_one_level_deep() -> None:
    result = evaluate(_OPS["createWithdraw"], {"body": {"to": "acct-2"}})
    assert result.status == 422
    assert "schema.required" in result.signals


def test_well_formed_call_synthesizes_a_success_from_the_spec() -> None:
    result = evaluate(_OPS["getBalance"], {"account": "a", "currency": "USD"})
    assert result.status == 200
    assert result.signals == []
    assert result.remediation == {}
    assert result.data == {"balance": 0}


def test_missing_error_schema_falls_back_to_a_generic_constant_body() -> None:
    # createWithdraw declares no error response schema: the fallback body is a
    # code constant (control-plane safe), never an interpolated arg value.
    result = evaluate(_OPS["createWithdraw"], {})
    assert result.status == 422
    assert isinstance(result.data, dict)
    assert "acct" not in str(result.data)


def test_mode_note_marks_the_result_synthetic() -> None:
    for args in ({}, {"account": "a"}):
        result = evaluate(_OPS["getBalance"], args)
        assert "synthetic" in result.mode_note.lower()
        assert "no live call" in result.mode_note.lower()


def test_sandbox_never_writes_the_corpus() -> None:
    # Control-plane gate (invariant #1): no corpus import / record call site may
    # ever appear in the sandbox module. Structural, not behavioral — grep the source.
    src = Path(sandbox.__file__).read_text(encoding="utf-8")
    assert "corpus.record" not in src
    assert re.search(r"^\s*(from|import)\s+\S*corpus", src, re.MULTILINE) is None
