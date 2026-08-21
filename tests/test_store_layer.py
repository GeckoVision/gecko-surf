"""The Mongo store boundary: allowlist-first writes, observed-only scores.

The load-bearing properties (architecture §2, §3): the store CANNOT hold a
payload (the only writer runs the corpus allowlist first); a plaintext key can
never reach it; the published score reads observed rows only; a rate below the
N-floor is None, never 0.0.
"""

from __future__ import annotations

import pytest

from gecko.corpus import CallOutcome, OUTCOME_SOURCES
from gecko.store import (
    N_FLOOR,
    InMemoryCollection,
    ProjectionError,
    endpoint_score,
    record_call_outcome,
)

HASH = "a" * 64  # an opaque key hash (never a plaintext gecko_sk_)


def _outcome(
    *, fcc: bool = True, source: str = "observed", op: str = "make_purchase"
) -> CallOutcome:
    return CallOutcome(
        ts=0,
        surface_id="orquestra:let_me_buy",
        surface_rev="rev1",
        operation_id=op,
        method="POST",
        path_template="/purchase/{store}",
        params_present=["store", "product"],
        arg_shape={"store": "string", "product": "string"},
        body_present=False,
        status=200,
        ok=True,
        error_class="",
        first_call_correct=fcc,
        attempt=1,
        latency_ms=12,
        mode="recorded",
        auth_injected=False,
        source=source,
    )


def test_source_is_a_valid_closed_vocabulary_member() -> None:
    assert "observed" in OUTCOME_SOURCES  # the filter scores.py depends on


# ----------------------------------------------------------------- projections
def test_record_writes_an_allowlisted_row_with_ids() -> None:
    col = InMemoryCollection()
    row = record_call_outcome(col, _outcome(), run_id="run-1", api_key_id=HASH)
    assert row["operation_id"] == "make_purchase"
    assert row["run_id"] == "run-1" and row["api_key_id"] == HASH
    assert col.count_documents({"surface_id": "orquestra:let_me_buy"}) == 1


def test_row_carries_no_payload_fields() -> None:
    # The allowlist has no field for values/bodies/secrets — so none can appear.
    col = InMemoryCollection()
    row = record_call_outcome(col, _outcome(), run_id="r", api_key_id=HASH)
    forbidden = {"body", "params", "response", "url", "authorization", "token", "value"}
    assert forbidden.isdisjoint(row.keys())
    # arg_shape carries TYPES only, never values.
    assert set(row["arg_shape"].values()) <= {
        "string",
        "number",
        "boolean",
        "object",
        "array",
        "null",
    }


def test_plaintext_key_is_refused() -> None:
    col = InMemoryCollection()
    with pytest.raises(ProjectionError, match="PLAINTEXT"):
        record_call_outcome(
            col, _outcome(), run_id="r", api_key_id="gecko_sk_" + "x" * 43
        )


def test_missing_attribution_is_refused() -> None:
    col = InMemoryCollection()
    with pytest.raises(ProjectionError):
        record_call_outcome(col, _outcome(), run_id="", api_key_id=HASH)
    with pytest.raises(ProjectionError):
        record_call_outcome(col, _outcome(), run_id="r", api_key_id="")


def test_source_is_not_a_writer_parameter() -> None:
    # record_call_outcome takes source from the outcome, never as an arg — so a
    # caller cannot relabel a synthetic row as observed at write time.
    import inspect

    assert "source" not in inspect.signature(record_call_outcome).parameters


# ---------------------------------------------------------------------- scores
def _seed(
    col: InMemoryCollection, *, observed_fcc: int, observed_total: int, synthetic: int
) -> None:
    for i in range(observed_total):
        record_call_outcome(
            col, _outcome(fcc=i < observed_fcc), run_id="r", api_key_id=HASH
        )
    for _ in range(synthetic):
        record_call_outcome(
            col, _outcome(fcc=True, source="synthetic"), run_id="r", api_key_id=HASH
        )


def test_score_reads_observed_only() -> None:
    col = InMemoryCollection()
    # 20 observed (all fcc) + 50 synthetic (all fcc). Only the 20 observed count.
    _seed(col, observed_fcc=20, observed_total=20, synthetic=50)
    score = endpoint_score(
        col,
        surface_id="orquestra:let_me_buy",
        operation_id="make_purchase",
        spec_rev="rev1",
    )
    assert score.n == 20
    assert score.first_call_correct == 1.0


def test_rate_below_floor_is_none_not_zero() -> None:
    col = InMemoryCollection()
    _seed(
        col, observed_fcc=0, observed_total=N_FLOOR - 1, synthetic=0
    )  # 19 observed, all fail
    score = endpoint_score(
        col,
        surface_id="orquestra:let_me_buy",
        operation_id="make_purchase",
        spec_rev="rev1",
    )
    assert score.n == N_FLOOR - 1
    assert score.first_call_correct is None  # NOT 0.0 — not enough to say


def test_rate_computed_at_and_above_floor() -> None:
    col = InMemoryCollection()
    _seed(col, observed_fcc=15, observed_total=N_FLOOR, synthetic=0)  # 15/20
    score = endpoint_score(
        col,
        surface_id="orquestra:let_me_buy",
        operation_id="make_purchase",
        spec_rev="rev1",
    )
    assert score.first_call_correct == 0.75


def test_score_pinned_to_spec_rev() -> None:
    col = InMemoryCollection()
    _seed(col, observed_fcc=20, observed_total=20, synthetic=0)  # all at rev1
    other = endpoint_score(
        col,
        surface_id="orquestra:let_me_buy",
        operation_id="make_purchase",
        spec_rev="rev2",
    )
    assert (
        other.n == 0 and other.first_call_correct is None
    )  # a different rev sees nothing


def test_unfilled_vector_slots_are_none() -> None:
    col = InMemoryCollection()
    _seed(col, observed_fcc=20, observed_total=20, synthetic=0)
    score = endpoint_score(
        col,
        surface_id="orquestra:let_me_buy",
        operation_id="make_purchase",
        spec_rev="rev1",
    )
    # v1 fills FCC; the other feeds are not-yet-evaluated, not zero.
    assert score.routability is None
    assert score.derive_readiness is None
    assert score.simulate_land is None
    assert score.refusal is None
