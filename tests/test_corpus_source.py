"""Provenance rails for the correctness corpus — the two-axis foundation.

The feedback-capture decision (private/2026-07-02-feedback-capture-decision.md)
bakes in TWO orthogonal, one-way axes so an unlabeled synthetic stream can never
corrupt the moat metric:

* ``source`` (observed | reported | synthetic) — HOW the outcome was obtained.
  DERIVED at the ``outcome_from`` boundary from the capture ``mode`` ("did status
  come from the wire?"), never free-set by a caller. Governs routing/metric.
* ``tenancy`` (local | contributed) — may the record egress into the cross-customer
  corpus? Default ``local``; the egress layer is NOT built here — only the field.

These tests are the same non-negotiable gate as ``test_corpus_controlplane``: a
control-plane violation is a build break. They also cover the (future) REPORTED
path — asserting it still passes through the body-proof ``outcome_from`` boundary,
so no payload can enter even when the agent supplies the claimed outcome.
"""

from __future__ import annotations

import inspect
import json

import pytest

from gecko.corpus import (
    ALLOWED_KEYS,
    OUTCOME_SOURCES,
    TENANCIES,
    CallOutcome,
    CorpusError,
    error_class_for,
    outcome_from,
    record,
    source_for_mode,
    synthetic_sibling,
    to_record,
)

# A realistic templated _invoke + args carrying sensitive VALUES the corpus must
# never persist — same fixtures as test_corpus_controlplane, reused for the reported
# path so the body-proof guarantee is proven identically across sources.
TOOL_INVOKE = {
    "method": "GET",
    "path": "/v1/assets/by-mint/{mint}/state",
    "param_locations": {"mint": "path", "limit": "query"},
}
SENSITIVE_MINT = "SoLSeCrEtMintAddr1111111111111111111111111"
SENSITIVE_BODY_VALUE = "topsecret-user-note-DO-NOT-PERSIST"
ARGS = {"mint": SENSITIVE_MINT, "limit": 50, "body": {"note": SENSITIVE_BODY_VALUE}}


def _outcome(*, mode: str, **overrides) -> CallOutcome:
    base = dict(
        operation_id="get_asset_state",
        tool_invoke=TOOL_INVOKE,
        args=ARGS,
        status=200,
        error_class="none",
        latency_ms=42,
        mode=mode,
        auth_injected=True,
        ts=1_700_000_000_000,
        surface_id="pegana",
        surface_rev="rev-1",
    )
    base.update(overrides)
    return outcome_from(**base)


# --------------------------------------------------------------------------- #
# source is DERIVED from mode — "did status come from the wire?"
# --------------------------------------------------------------------------- #
def test_source_for_mode_derivation():
    assert source_for_mode("live") == "observed"  # real upstream status off the wire
    assert source_for_mode("recorded") == "synthetic"  # faked 200, never observed
    assert source_for_mode("reported") == "reported"  # agent-claimed status
    # Fail closed: an unknown mode collapses to the non-published bucket, never observed.
    assert source_for_mode("weird") == "synthetic"
    assert source_for_mode("") == "synthetic"


def test_every_derived_source_is_a_closed_set_member():
    for mode in ("live", "recorded", "reported", "anything"):
        assert source_for_mode(mode) in OUTCOME_SOURCES


def test_outcome_from_derives_source_from_mode():
    assert _outcome(mode="live").source == "observed"
    assert _outcome(mode="recorded").source == "synthetic"
    assert _outcome(mode="reported").source == "reported"


def test_caller_cannot_free_set_source():
    # The whole point of deriving: a caller must not be able to pass a wrong source.
    params = set(inspect.signature(outcome_from).parameters)
    assert "source" not in params
    with pytest.raises(TypeError):
        outcome_from(  # type: ignore[call-arg]
            operation_id="x",
            tool_invoke=TOOL_INVOKE,
            args={"mint": "v"},
            status=200,
            error_class="none",
            latency_ms=1,
            mode="recorded",
            auth_injected=False,
            ts=1,
            surface_id="s",
            surface_rev="r",
            source="observed",  # trying to lie: recorded is synthetic, not observed
        )


# --------------------------------------------------------------------------- #
# the error_class_for(None, None) mislabel bug
# --------------------------------------------------------------------------- #
def test_error_class_none_none_is_none_not_other():
    # A well-formed synthetic (no status, no exception) is NOT a failure. Before the
    # fix it fell through to "other" and mislabeled every synthetic success.
    assert error_class_for(None, None) == "none"


def test_error_class_none_with_real_exc_still_other():
    # Regression guard: only (None, None) becomes "none"; a real unknown exc stays "other".
    assert error_class_for(None, RuntimeError("boom")) == "other"


# --------------------------------------------------------------------------- #
# routing: synthetic segregates by FILE (fails closed), observed/reported stay
# --------------------------------------------------------------------------- #
def test_synthetic_routes_to_segregated_sibling(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    record(_outcome(mode="recorded"), corpus)  # synthetic
    sibling = synthetic_sibling(corpus)
    assert sibling.name == "synthetic.jsonl"
    assert sibling.exists()  # the synthetic row landed in the segregated file
    assert not corpus.exists()  # and NEVER in the main corpus (fails closed)
    row = json.loads(sibling.read_text().strip())
    assert row["source"] == "synthetic"


def test_observed_stays_in_main_corpus(tmp_path):
    corpus = tmp_path / "corpus.jsonl"
    record(_outcome(mode="live"), corpus)  # observed
    assert corpus.exists()
    assert not synthetic_sibling(corpus).exists()
    assert json.loads(corpus.read_text().strip())["source"] == "observed"


def test_reported_stays_in_main_corpus(tmp_path):
    # Reported lives in the main corpus (source split is IN-BAND there); only synthetic
    # is segregated by file. Reported is excluded from the FCC rate downstream, not here.
    corpus = tmp_path / "corpus.jsonl"
    record(_outcome(mode="reported"), corpus)
    assert corpus.exists()
    assert not synthetic_sibling(corpus).exists()
    assert json.loads(corpus.read_text().strip())["source"] == "reported"


# --------------------------------------------------------------------------- #
# tenancy axis: reserved now, egress layer NOT built — field + default only
# --------------------------------------------------------------------------- #
def test_tenancy_defaults_to_local():
    assert _outcome(mode="live").tenancy == "local"


def test_tenancy_is_a_closed_set_validated_fail_closed():
    assert "local" in TENANCIES and "contributed" in TENANCIES
    with pytest.raises(CorpusError):
        _outcome(mode="live", tenancy="everyone")  # off-set egress label -> reject


def test_source_and_tenancy_are_allowlisted_and_persisted(tmp_path):
    # The allowlist IS the schema; both new axes are justified fields on every record.
    assert {"source", "tenancy"} <= ALLOWED_KEYS
    corpus = tmp_path / "corpus.jsonl"
    record(_outcome(mode="live"), corpus)
    row = json.loads(corpus.read_text().strip())
    assert set(row) == ALLOWED_KEYS  # full record, nothing dropped, nothing extra
    assert row["source"] == "observed"
    assert row["tenancy"] == "local"


# --------------------------------------------------------------------------- #
# REPORTED-path control-plane gate (item 5) — mirrors test_corpus_controlplane.
# The agent-report tool is NOT built; this proves the boundary holds for it anyway.
# --------------------------------------------------------------------------- #
def test_reported_outcome_from_signature_cannot_accept_body():
    # Same boundary proof as observed: no parameter through which a body could enter.
    params = set(inspect.signature(outcome_from).parameters)
    assert {"data", "result", "response", "body", "url"}.isdisjoint(params)


def test_reported_outcome_persists_no_value_substring(tmp_path):
    # The killer test on the REPORTED source: even a caller passing sensitive args gets
    # only categorical metadata on disk — no payload/param VALUE, no filled URL.
    corpus = tmp_path / "corpus.jsonl"
    out = _outcome(mode="reported")
    assert out.source == "reported"
    record(out, corpus)
    raw = corpus.read_text()
    assert SENSITIVE_MINT not in raw
    assert SENSITIVE_BODY_VALUE not in raw
    assert "/v1/assets/by-mint/SoL" not in raw  # no filled URL
    row = json.loads(raw.strip())
    assert set(row) <= ALLOWED_KEYS  # nothing off-schema slipped in
    assert (
        row["path_template"] == "/v1/assets/by-mint/{mint}/state"
    )  # template, not filled


def test_reported_record_is_re_gated_by_the_allowlist():
    # Belt-and-suspenders: a tampered reported record with a smuggled body is rejected.
    from gecko.corpus import assert_allowlisted

    tampered = to_record(_outcome(mode="reported"))
    tampered["data"] = "a response body sneaking in"
    with pytest.raises(CorpusError):
        assert_allowlisted(tampered)
