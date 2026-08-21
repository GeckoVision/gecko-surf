"""The recorded lane: simulate → attributed SimulatedOutcome → simulate/land score.

Load-bearing properties: the recorded green is a first_call_correct=True on a
clean simulate; the row is source="simulated" (funnel/simulate tier, NEVER the
observed FCC rate); it is payload-free; a plaintext key is refused; and it lands
on the score's simulate/land rate for the matching endpoint.
"""

from __future__ import annotations

import pytest

from gecko.recorded_lane import PreparedSim, run_recorded
from gecko.simulate import Receipt
from gecko.store import InMemoryCollection, ProjectionError, endpoint_score

SURFACE = "orquestra:let_me_buy"
PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
HASH = "b" * 64


def _receipt(status: str, revert_class: str | None) -> Receipt:
    return Receipt(
        status=status,  # type: ignore[arg-type]
        err=None,
        revert_class=revert_class,
        units_consumed=37088,
        sol_delta=-5000,
        tokens_received=1,
        logs_tail=(),
        network_label="simulated against mainnet",
    )


def _prepare(status: str = "pass", revert_class: str | None = None):
    def prepare() -> PreparedSim:
        return PreparedSim(
            receipt=_receipt(status, revert_class),
            recipe_hash="c" * 64,
            slot=440_000_000,
            network="mainnet",
        )

    return prepare


def _run(outcomes: InMemoryCollection, prepare, *, api_key_id: str = HASH):
    return run_recorded(
        prepare,
        outcomes,
        surface_id=SURFACE,
        program_id=PROGRAM,
        instruction="make_purchase",
        api_key_id=api_key_id,
        run_id="run-rec-1",
        ts=0,
    )


def test_clean_simulate_is_the_recorded_green() -> None:
    outcomes = InMemoryCollection()
    run = _run(outcomes, _prepare(status="pass"))
    assert run.first_call_correct is True and run.recorded is True
    assert run.status == "pass" and run.units_consumed == 37088


def test_a_revert_is_not_a_green() -> None:
    outcomes = InMemoryCollection()
    run = _run(outcomes, _prepare(status="fail", revert_class="constraint-seeds"))
    assert run.first_call_correct is False and run.status == "fail"


def test_recorded_row_is_simulated_source_not_observed() -> None:
    outcomes = InMemoryCollection()
    _run(outcomes, _prepare())
    assert outcomes.count_documents({"source": "simulated"}) == 1
    assert outcomes.count_documents({"source": "observed"}) == 0


def test_recorded_row_is_payload_free() -> None:
    outcomes = InMemoryCollection()
    _run(outcomes, _prepare())
    row = next(outcomes.find({"source": "simulated"}))
    forbidden = {
        "args",
        "body",
        "response",
        "logs",
        "unsigned_transaction",
        "value",
        "url",
    }
    assert forbidden.isdisjoint(row.keys())
    assert row["instruction"] == "make_purchase"  # a NAME, never args


def test_plaintext_key_is_refused() -> None:
    outcomes = InMemoryCollection()
    with pytest.raises(ProjectionError, match="PLAINTEXT"):
        _run(outcomes, _prepare(), api_key_id="gecko_sk_" + "x" * 43)


def test_recorded_runs_feed_the_simulate_land_rate() -> None:
    outcomes = InMemoryCollection()
    # 20 clean recorded runs → simulate/land = 1.0; FCC stays None (no observed).
    for _ in range(20):
        _run(outcomes, _prepare(status="pass"))
    score = endpoint_score(
        outcomes, surface_id=SURFACE, operation_id="make_purchase", spec_rev="anyrev"
    )
    assert score.simulate_land == 1.0 and score.n_simulated == 20
    assert score.first_call_correct is None  # simulated never inflates observed FCC


def test_simulate_land_mixes_pass_and_fail() -> None:
    outcomes = InMemoryCollection()
    for _ in range(15):
        _run(outcomes, _prepare(status="pass"))
    for _ in range(5):
        _run(outcomes, _prepare(status="fail", revert_class="constraint-seeds"))
    score = endpoint_score(
        outcomes, surface_id=SURFACE, operation_id="make_purchase", spec_rev="anyrev"
    )
    assert score.simulate_land == 0.75 and score.n_simulated == 20
