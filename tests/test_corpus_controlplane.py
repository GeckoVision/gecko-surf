"""Phase-0 control-plane gate for the correctness corpus.

These tests are the spec's non-negotiable gate (docs/superpowers/specs/
2026-06-28-correctness-corpus-design.md §6): a control-plane violation is a
build break, not a review comment. They assert the corpus writer persists ONLY
allowlisted metadata and that no param/path/body VALUE, response body, or token
can reach the file — by construction (`outcome_from` never accepts the body) and
by the writer's reject-unknown-key allowlist.
"""

from __future__ import annotations

import json

import pytest

from gecko.caller import CallError
from gecko.corpus import (
    ALLOWED_KEYS,
    ERROR_CLASSES,
    CallOutcome,
    CorpusError,
    error_class_for,
    outcome_from,
    record,
    to_record,
)

# A realistic tool _invoke (templated path) + args carrying sensitive VALUES the
# corpus must never persist: a wallet-like identifier and a secret-looking body.
TOOL_INVOKE = {
    "method": "GET",
    "path": "/v1/assets/by-mint/{mint}/state",
    "param_locations": {"mint": "path", "limit": "query"},
}
SENSITIVE_MINT = "SoLSeCrEtMintAddr1111111111111111111111111"
SENSITIVE_BODY_VALUE = "topsecret-user-note-DO-NOT-PERSIST"
ARGS = {
    "mint": SENSITIVE_MINT,
    "limit": 50,
    "body": {"note": SENSITIVE_BODY_VALUE},
}


def _make_outcome(**overrides) -> CallOutcome:
    base = dict(
        operation_id="get_asset_state",
        tool_invoke=TOOL_INVOKE,
        args=ARGS,
        status=200,
        error_class="none",
        latency_ms=42,
        mode="live",
        auth_injected=True,
        ts=1_700_000_000_000,
        surface_id="pegana",
        surface_rev="rev-1",
    )
    base.update(overrides)
    return outcome_from(**base)


def test_allowlist_matches_dataclass_fields_exactly():
    # The allowlist IS the schema — every persisted key is justified in §1.
    field_names = {f for f in CallOutcome.__dataclass_fields__}
    assert ALLOWED_KEYS == field_names


def test_recorded_record_keys_are_all_allowlisted(tmp_path):
    out = _make_outcome()
    path = tmp_path / "corpus.jsonl"
    record(out, path)
    line = json.loads(path.read_text().strip())
    assert set(line) <= ALLOWED_KEYS
    assert set(line) == ALLOWED_KEYS  # full record, nothing silently dropped


def test_no_value_path_body_or_token_substring_leaks(tmp_path):
    # The killer test: scan the raw file; no VALUE the agent supplied may appear.
    out = _make_outcome()
    path = tmp_path / "corpus.jsonl"
    record(out, path)
    raw = path.read_text()
    assert SENSITIVE_MINT not in raw  # path param value
    assert SENSITIVE_BODY_VALUE not in raw  # request body value
    assert "/v1/assets/by-mint/SoL" not in raw  # no filled URL
    assert (
        "50" not in raw or '"limit"' not in raw
    )  # the literal value 50 not stored as a value


def test_path_template_is_templated_not_filled(tmp_path):
    out = _make_outcome()
    path = tmp_path / "corpus.jsonl"
    record(out, path)
    line = json.loads(path.read_text().strip())
    assert "{" in line["path_template"]  # proves a template, not a filled path
    assert line["path_template"] == "/v1/assets/by-mint/{mint}/state"


def test_params_present_are_names_only_no_values():
    out = _make_outcome()
    assert set(out.params_present) == {"mint", "limit"}  # 'body' excluded
    assert SENSITIVE_MINT not in out.params_present


def test_arg_shape_is_json_types_not_values():
    out = _make_outcome()
    assert out.arg_shape == {"mint": "string", "limit": "integer"}
    assert out.body_present is True
    assert SENSITIVE_MINT not in json.dumps(out.arg_shape)


def test_to_record_rejects_unknown_key():
    # Fail closed: a future careless field must break the build, not leak.
    out = _make_outcome()
    tampered = to_record(out)
    tampered["data"] = "a response body sneaking in"
    with pytest.raises(CorpusError):
        # re-validating a tampered mapping must reject the non-allowlisted key
        from gecko.corpus import assert_allowlisted

        assert_allowlisted(tampered)


def test_outcome_from_signature_cannot_accept_body():
    # Boundary proof: there is no parameter through which a response body enters.
    import inspect

    params = set(inspect.signature(outcome_from).parameters)
    assert "data" not in params and "result" not in params and "response" not in params


@pytest.mark.parametrize(
    "status,exc,expected",
    [
        (200, None, "none"),
        (401, None, "unauthorized_401"),
        (403, None, "forbidden_403"),
        (404, None, "not_found_404"),
        (422, None, "unprocessable_422"),
        (429, None, "rate_limited_429"),
        (500, None, "server_5xx"),
        (
            None,
            CallError("missing required path parameter(s): mint"),
            "missing_required_param",
        ),
        (None, TimeoutError("timed out"), "timeout"),
        (None, RuntimeError("boom"), "other"),
    ],
)
def test_error_class_mapping(status, exc, expected):
    assert error_class_for(status, exc) == expected
    assert expected in ERROR_CLASSES


def test_first_call_correct_semantics():
    assert _make_outcome(status=200, attempt=1).first_call_correct is True
    assert _make_outcome(status=200, attempt=2).first_call_correct is False
    assert (
        _make_outcome(
            status=500, error_class="server_5xx", attempt=1
        ).first_call_correct
        is False
    )


def test_preflight_failure_has_null_status_and_not_ok():
    out = _make_outcome(
        status=None, error_class="missing_required_param", latency_ms=None
    )
    assert out.status is None
    assert out.ok is False
    assert out.first_call_correct is False
