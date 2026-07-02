"""Control-plane gate + aggregation correctness for opt-out usage telemetry.

Telemetry COMPOSES on the correctness corpus (``gecko.corpus``): it only ever reads
the already-control-plane-safe ``CallOutcome`` records and emits ANONYMIZED COUNTS.
These tests mirror ``test_corpus_controlplane.py``'s rigor — a control-plane
violation is a build break, not a review comment:

1. The emitted ``TelemetryPayload`` carries ONLY allowlisted aggregate keys +
   install_id + version + ts — no surface name, no path, no per-call detail, no
   value/token (by construction: the payload has no field that could hold one, and
   the writer fails closed on any non-allowlisted key — including a free-text
   ``error_class`` that could smuggle a value).
2. Phone-home ships DISABLED (opt-in sink; ``GECKO_TELEMETRY=off`` hard-disables).
3. Telemetry never raises into the call path — except a control-plane violation,
   which MUST surface.
"""

from __future__ import annotations

import json
import uuid

import pytest

from gecko import __version__
from gecko import corpus
from gecko.telemetry import (
    PAYLOAD_ALLOWED_KEYS,
    TelemetryError,
    TelemetryPayload,
    UsageAggregate,
    aggregate,
    assert_payload_allowlisted,
    build_payload,
    default_install_id_path,
    read_or_create_install_id,
    report,
    to_payload_record,
)

# --- sensitive VALUES that must NEVER reach a telemetry payload (mirror corpus) ---
TOOL_INVOKE = {"method": "GET", "path": "/v1/assets/by-mint/{mint}/state"}
SENSITIVE_MINT = "SoLSeCrEtMintAddr1111111111111111111111111"
SENSITIVE_BODY_VALUE = "topsecret-user-note-DO-NOT-PERSIST"
SENSITIVE_ARGS = {
    "mint": SENSITIVE_MINT,
    "limit": 50,
    "body": {"note": SENSITIVE_BODY_VALUE},
}


def _outcome(
    surface_id: str,
    surface_rev: str,
    status: int | None,
    error_class: str,
    *,
    attempt: int = 1,
    operation_id: str = "op",
    tool_invoke: dict | None = None,
    args: dict | None = None,
    mode: str = "live",  # default OBSERVED: aggregate's real-usage metrics count these
) -> corpus.CallOutcome:
    return corpus.outcome_from(
        operation_id=operation_id,
        tool_invoke=tool_invoke or {"method": "GET", "path": "/x/{id}"},
        args=args or {"id": "v"},
        status=status,
        error_class=error_class,
        latency_ms=1,
        mode=mode,
        auth_injected=False,
        ts=1,
        surface_id=surface_id,
        surface_rev=surface_rev,
        attempt=attempt,
    )


def _write(path, outcomes: list[corpus.CallOutcome]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for o in outcomes:
            fh.write(json.dumps(corpus.to_record(o)) + "\n")


# A 5-record corpus with known, hand-countable metrics.
def _sample_corpus(path) -> None:
    _write(
        path,
        [
            _outcome("alpha", "r1", 200, "none"),  # fcc True
            _outcome("alpha", "r1", 200, "none"),  # fcc True
            _outcome("alpha", "r2", 404, "not_found_404"),  # drift r1->r2, fcc False
            _outcome("beta", "rb", 200, "none"),  # fcc True
            _outcome("beta", "rb", 500, "server_5xx"),  # fcc False
        ],
    )


def _payload(**over) -> TelemetryPayload:
    base = dict(
        install_id="00000000-0000-4000-8000-000000000000",
        version="0.1.0",
        ts=1,
        surfaces_comprehended=1,
        surface_revs=1,
        total_calls=1,
        first_call_correct_rate=1.0,
        error_class_distribution={"none": 1},
        drift_events=0,
    )
    base.update(over)
    return TelemetryPayload(**base)  # type: ignore[arg-type]


class _FakeSink:
    """An injected sink — the opt-in phone-home seam, recorded for assertions."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, record) -> None:
        self.calls.append(record)


# --------------------------------------------------------------------------- #
# Aggregation correctness
# --------------------------------------------------------------------------- #
def test_aggregate_counts_from_corpus(tmp_path):
    p = tmp_path / "corpus.jsonl"
    _sample_corpus(p)
    agg = aggregate(p)
    assert isinstance(agg, UsageAggregate)
    assert agg.surfaces_comprehended == 2  # alpha, beta
    assert agg.surface_revs == 3  # r1, r2, rb
    assert agg.total_calls == 5
    assert agg.first_call_correct_rate == 0.6  # 3 of 5
    assert agg.error_class_distribution == {
        "none": 3,
        "not_found_404": 1,
        "server_5xx": 1,
    }
    assert agg.drift_events == 1  # alpha r1->r2; beta no change


def test_aggregate_excludes_synthetic_and_reported_from_the_rate(tmp_path):
    # The moat-metric fix, end to end. A file mixing all three sources (bypassing the
    # by-file segregation on purpose, to prove the belt-and-suspenders filter):
    p = tmp_path / "corpus.jsonl"
    _write(
        p,
        [
            _outcome("alpha", "r1", 200, "none", mode="live"),  # observed, fcc True
            _outcome(
                "alpha", "r1", 500, "server_5xx", mode="live"
            ),  # observed, fcc False
            _outcome(
                "alpha", "r1", 200, "none", mode="recorded"
            ),  # SYNTHETIC faked 200
            _outcome("alpha", "r1", 200, "none", mode="reported"),  # REPORTED (biased)
        ],
    )
    agg = aggregate(p)
    # BEFORE the fix this rate would be 3/4 = 0.75 (the faked recorded 200 + the reported
    # 200 both counted as first_call_correct). AFTER: observed-only = 2 rows, 1 correct.
    assert agg.total_calls == 2  # only the two observed calls
    assert agg.first_call_correct_rate == 0.5  # NOT dragged to 0.75 by the fake success
    assert agg.error_class_distribution == {
        "none": 1,
        "server_5xx": 1,
    }  # no fake "none"


def test_aggregate_missing_or_empty_corpus_is_zeros(tmp_path):
    missing = aggregate(tmp_path / "nope.jsonl")
    assert missing == UsageAggregate(0, 0, 0, 0.0, {}, 0)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert aggregate(empty) == UsageAggregate(0, 0, 0, 0.0, {}, 0)


def test_aggregate_skips_malformed_lines(tmp_path):
    p = tmp_path / "corpus.jsonl"
    _write(p, [_outcome("alpha", "r1", 200, "none")])
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("not json\n\n")
    agg = aggregate(p)
    assert agg.total_calls == 1  # the junk line is ignored, best-effort


def test_drift_events_counts_each_rev_transition(tmp_path):
    p = tmp_path / "corpus.jsonl"
    _write(
        p,
        [
            _outcome("alpha", "r1", 200, "none"),
            _outcome("alpha", "r2", 200, "none"),  # change 1
            _outcome("alpha", "r1", 200, "none"),  # change 2 (rolled back)
        ],
    )
    assert aggregate(p).drift_events == 2


# --------------------------------------------------------------------------- #
# Control-plane gate (the important ones)
# --------------------------------------------------------------------------- #
def test_payload_allowlist_matches_dataclass_fields_exactly():
    assert PAYLOAD_ALLOWED_KEYS == set(TelemetryPayload.__dataclass_fields__)


def test_payload_schema_has_no_surface_name_path_or_percall_field():
    # Telemetry is counts only — these corpus fields must NOT exist on the payload.
    forbidden = {
        "surface_id",
        "surface_rev",
        "path_template",
        "operation_id",
        "params_present",
        "arg_shape",
        "body_present",
        "method",
        "status",
        "data",
        "result",
        "response",
    }
    assert forbidden.isdisjoint(PAYLOAD_ALLOWED_KEYS)


def test_emitted_record_keys_are_exactly_allowlisted():
    record = to_payload_record(_payload())
    assert set(record) == PAYLOAD_ALLOWED_KEYS


def test_assert_payload_allowlisted_rejects_unknown_key():
    record = to_payload_record(_payload())
    record["data"] = "a response body sneaking in"  # the classic leak
    with pytest.raises(TelemetryError):
        assert_payload_allowlisted(record)


def test_assert_payload_allowlisted_rejects_free_text_error_class():
    # error_class keys could otherwise smuggle a VALUE; they must be the closed set.
    record = to_payload_record(_payload())
    record["error_class_distribution"] = {SENSITIVE_MINT: 1}
    with pytest.raises(TelemetryError):
        assert_payload_allowlisted(record)


def test_no_value_path_or_token_substring_leaks_into_payload(tmp_path):
    # The killer test: build a payload from a corpus full of sensitive VALUES and a
    # real path template, then scan the serialized payload — nothing identifying or
    # value-bearing may appear.
    p = tmp_path / "corpus.jsonl"
    _write(
        p,
        [
            _outcome(
                "pegana",
                "rev-secret-7",
                200,
                "none",
                operation_id="get_asset_state",
                tool_invoke=TOOL_INVOKE,
                args=SENSITIVE_ARGS,
            )
        ],
    )
    payload = build_payload(
        p, install_id_path=tmp_path / "install-id", ts=123, version="0.1.0"
    )
    raw = json.dumps(to_payload_record(payload))
    assert SENSITIVE_MINT not in raw  # path param value
    assert SENSITIVE_BODY_VALUE not in raw  # body value
    assert "/v1/assets" not in raw  # no path template, no filled URL
    assert "pegana" not in raw  # no surface name (counts only)
    assert "rev-secret-7" not in raw  # no surface rev string
    assert "get_asset_state" not in raw  # no operation id


# --------------------------------------------------------------------------- #
# Opt-out + default-disabled posture
# --------------------------------------------------------------------------- #
def test_report_default_sink_ships_disabled():
    # No injected sink => no phone-home. report returns False (nothing was sent).
    assert report(_payload()) is False


def test_report_with_injected_sink_phones_home_allowlisted_only():
    sink = _FakeSink()
    assert report(_payload(), sink=sink) is True
    assert len(sink.calls) == 1
    assert set(sink.calls[0]) == PAYLOAD_ALLOWED_KEYS  # only allowlisted keys reach it


def test_report_opt_out_disables_even_an_injected_sink(monkeypatch):
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    sink = _FakeSink()
    assert report(_payload(), sink=sink) is False
    assert sink.calls == []


def test_report_swallows_generic_sink_error(monkeypatch):
    # Best-effort: a sink failure (e.g. network down) must NEVER raise into the call.
    def boom(_record) -> None:
        raise RuntimeError("network down")

    assert report(_payload(), sink=boom) is False


def test_report_does_not_swallow_control_plane_violation():
    # A control-plane/allowlist violation MUST surface, same as corpus.record.
    def violating(_record) -> None:
        raise TelemetryError("non-allowlisted key downstream")

    with pytest.raises(TelemetryError):
        report(_payload(), sink=violating)


# --------------------------------------------------------------------------- #
# install_id: opaque, persisted once, stable
# --------------------------------------------------------------------------- #
def test_install_id_read_or_create_is_stable(tmp_path):
    p = tmp_path / "install-id"
    first = read_or_create_install_id(p)
    second = read_or_create_install_id(p)
    assert first == second
    uuid.UUID(first)  # parses as a uuid -> opaque, not derived from anything PII
    assert p.read_text().strip() == first


def test_default_install_id_path_is_under_dot_gecko():
    path = default_install_id_path()
    assert path.name == "install-id"
    assert path.parent.name == ".gecko"


def test_build_payload_composes_aggregate_with_identity(tmp_path):
    p = tmp_path / "corpus.jsonl"
    _sample_corpus(p)
    payload = build_payload(p, install_id_path=tmp_path / "install-id", ts=999)
    assert payload.ts == 999
    assert payload.version == __version__
    uuid.UUID(payload.install_id)
    assert payload.total_calls == 5
    assert payload.surfaces_comprehended == 2
    assert payload.first_call_correct_rate == 0.6
