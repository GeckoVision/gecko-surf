"""The N-confirmed drift primitive (gecko/drift.py) — D2 §5, offline.

Groups categorical ``SimulatedOutcome`` rows by ``recipe_hash`` and flags a stable
``(status, revert_class)`` change across slots. A single divergent slot is noise, not
drift; the new class must hold on ``>= n_confirm`` DISTINCT slots.
"""

from __future__ import annotations

from gecko.corpus import SimulatedOutcome
from gecko.drift import DriftEvent, detect_drift


def _row(
    *,
    status: str = "pass",
    revert_class: str = "none",
    slot: int | None = 1,
    recipe_hash: str = "H",
    program_id: str = "P",
    instruction: str = "buy",
) -> SimulatedOutcome:
    return SimulatedOutcome(
        ts=1_700_000_000_000,
        surface_id="orquestra:P",
        program_id=program_id,
        instruction=instruction,
        recipe_hash=recipe_hash,
        status=status,
        revert_class=revert_class,
        error_code=None,
        units_consumed=31000,
        slot=slot,
        network="fork",
        source="simulated",
    )


def test_stable_series_has_no_drift() -> None:
    rows = [_row(slot=s) for s in (1, 2, 3, 4)]
    assert detect_drift(rows) == []


def test_confirmed_flip_emits_exactly_one_event() -> None:
    rows = [
        _row(status="pass", slot=1),
        _row(status="pass", slot=2),
        _row(status="fail", revert_class="account_error", slot=3),
        _row(status="fail", revert_class="account_error", slot=4),
    ]
    events = detect_drift(rows, n_confirm=2)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, DriftEvent)
    assert ev.from_class == "pass:none"
    assert ev.to_class == "fail:account_error"
    assert ev.first_seen_slot == 3
    assert ev.confirmed_slot == 4
    assert ev.confirmations >= 2
    assert ev.recipe_hash == "H"
    assert ev.program_id == "P"
    assert ev.instruction == "buy"


def test_single_divergent_slot_is_noise() -> None:
    rows = [
        _row(status="pass", slot=1),
        _row(status="pass", slot=2),
        _row(status="fail", revert_class="account_error", slot=3),
        _row(status="pass", slot=4),  # baseline reasserts -> the fail was noise
    ]
    assert detect_drift(rows, n_confirm=2) == []


def test_repeated_same_slot_does_not_confirm() -> None:
    # Two fail rows on the SAME slot are ONE distinct slot -> not N-confirmed.
    rows = [
        _row(status="pass", slot=1),
        _row(status="fail", revert_class="account_error", slot=3),
        _row(status="fail", revert_class="account_error", slot=3),
    ]
    assert detect_drift(rows, n_confirm=2) == []


def test_fail_class_change_is_drift_too() -> None:
    # slippage -> account_error (both failing) is drift, not just pass->fail.
    rows = [
        _row(status="fail", revert_class="slippage", slot=1),
        _row(status="fail", revert_class="slippage", slot=2),
        _row(status="fail", revert_class="account_error", slot=3),
        _row(status="fail", revert_class="account_error", slot=4),
    ]
    events = detect_drift(rows, n_confirm=2)
    assert len(events) == 1
    assert events[0].from_class == "fail:slippage"
    assert events[0].to_class == "fail:account_error"


def test_groups_are_independent_by_recipe_hash() -> None:
    # A confirmed flip in recipe A must not be attributed across recipe B's stable series.
    rows = [
        _row(recipe_hash="A", status="pass", slot=1),
        _row(recipe_hash="A", status="fail", revert_class="account_error", slot=2),
        _row(recipe_hash="A", status="fail", revert_class="account_error", slot=3),
        _row(recipe_hash="B", status="pass", slot=1),
        _row(recipe_hash="B", status="pass", slot=2),
    ]
    events = detect_drift(rows, n_confirm=2)
    assert len(events) == 1
    assert events[0].recipe_hash == "A"
