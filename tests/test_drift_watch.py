"""The drift scheduler — proven without a clock, a network, or an RPC.

The whole point of this module is that it runs when nobody is watching, which is exactly
what makes it hard to test. So the two things that could rot are pure/injected: the
schedule decision is a function of three numbers, and the simulator is a seam. A test
here runs the equivalent of a week of watching in a millisecond.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko.drift_watch import (
    DEFAULT_INTERVAL_SECONDS,
    confirm_unavailable,
    WatchError,
    WatchPlan,
    WatchTarget,
    due,
    format_run,
    load_plan,
    run_once,
    watch,
)
from gecko.simulate import Receipt

HOUR = 3600


def _receipt(
    status: str = "pass", revert_class: str = "none", units: int = 50_000
) -> Receipt:
    return Receipt(
        status=status,
        err=None,
        revert_class=revert_class,
        units_consumed=units,
        sol_delta=None,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
    )


def _plan(tmp_path: Path, *, interval: int = 12 * HOUR) -> WatchPlan:
    return WatchPlan(
        targets=(
            WatchTarget(label="pump buy", program="pumpfun", instruction="buy"),
            WatchTarget(label="meteora swap", program="meteora", instruction="swap"),
        ),
        corpus_path=tmp_path / "corpus.jsonl",
        interval_seconds=interval,
    )


# --------------------------------------------------------------------------- #
# The schedule — three numbers, no clock.
# --------------------------------------------------------------------------- #
def test_the_first_pass_is_always_due() -> None:
    """A watch that waits a full interval before its first observation has no baseline to
    detect drift against — the first pass IS the baseline."""
    assert due(now=1_000.0, last_run=None, interval_seconds=DEFAULT_INTERVAL_SECONDS)


def test_a_pass_is_not_due_before_the_interval_elapses() -> None:
    assert not due(now=1_000.0 + HOUR, last_run=1_000.0, interval_seconds=12 * HOUR)


def test_a_pass_is_due_exactly_on_the_interval() -> None:
    assert due(now=1_000.0 + 12 * HOUR, last_run=1_000.0, interval_seconds=12 * HOUR)


def test_a_backwards_clock_runs_rather_than_stalling_the_watch() -> None:
    """A plan copied between machines, or a clock correction, must not silently freeze the
    watch until real time catches up. A spurious extra simulation is cheap; a stalled
    watch looks identical to a passing one."""
    assert due(now=1_000.0, last_run=9_999.0, interval_seconds=12 * HOUR)


# --------------------------------------------------------------------------- #
# A pass.
# --------------------------------------------------------------------------- #
def test_a_pass_simulates_every_target(tmp_path: Path) -> None:
    seen: list[str] = []

    def simulator(target: WatchTarget, _rpc: str, _call: Any, _record: Any) -> Receipt:
        seen.append(f"{target.program}/{target.instruction}")
        return _receipt()

    run = run_once(_plan(tmp_path), simulator=simulator, now=1_000.0)

    assert seen == ["pumpfun/buy", "meteora/swap"]
    assert all(r.ok for r in run.results)
    assert run.ran_at == 1_000


def test_one_broken_target_does_not_stop_the_others(tmp_path: Path) -> None:
    """A silent skip looks exactly like a passing call, so a failure is reported as a
    failure and the pass continues."""

    def simulator(target: WatchTarget, _rpc: str, _call: Any, _record: Any) -> Receipt:
        if target.program == "pumpfun":
            raise RuntimeError("no route today")
        return _receipt()

    run = run_once(_plan(tmp_path), simulator=simulator, now=1_000.0)

    assert len(run.failed) == 1
    assert run.failed[0].target.program == "pumpfun"
    assert run.failed[0].error == "RuntimeError"
    assert any(r.ok for r in run.results)  # meteora still ran


def test_a_missing_series_is_not_drift(tmp_path: Path) -> None:
    """Nothing recorded yet means no history — not a break."""
    run = run_once(
        _plan(tmp_path),
        simulator=lambda *_: _receipt(),
        now=1_000.0,
    )

    assert run.events == ()


# --------------------------------------------------------------------------- #
# The loop — a week of watching, instantly.
# --------------------------------------------------------------------------- #
def test_the_loop_runs_one_pass_per_interval(tmp_path: Path) -> None:
    ticks = iter([0.0, 1 * HOUR, 12 * HOUR, 13 * HOUR, 24 * HOUR, 25 * HOUR])
    passes: list[int] = []

    runs = watch(
        _plan(tmp_path),
        simulator=lambda *_: _receipt(),
        clock=lambda: next(ticks),
        sleep=lambda _seconds: passes.append(1),
        passes=3,
    )

    # Three passes at t=0, t=12h, t=24h — the hourly ticks in between are skipped.
    assert [run.ran_at for run in runs] == [0, 12 * HOUR, 24 * HOUR]


def test_the_loop_never_sleeps_longer_than_a_minute(tmp_path: Path) -> None:
    """A twelve-hour interval must not mean a twelve-hour uninterruptible sleep — the
    process has to stay responsive to a Ctrl-C."""
    slept: list[float] = []
    ticks = iter([0.0, 1.0, 2.0])

    watch(
        _plan(tmp_path, interval=12 * HOUR),
        simulator=lambda *_: _receipt(),
        clock=lambda: next(ticks),
        sleep=slept.append,
        passes=1,
    )

    assert all(seconds <= 60 for seconds in slept)


# --------------------------------------------------------------------------- #
# The plan file.
# --------------------------------------------------------------------------- #
def test_a_plan_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    path.write_text(
        json.dumps(
            {
                "corpus_path": str(tmp_path / "corpus.jsonl"),
                "interval_seconds": 3600,
                "n_confirm": 3,
                "targets": [
                    {
                        "label": "the exit",
                        "program": "jupiter",
                        "instruction": "route",
                        "bindings": {"amount": 10},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    plan = load_plan(path)

    assert plan.interval_seconds == 3600
    assert plan.n_confirm == 3
    assert plan.targets[0].label == "the exit"
    assert plan.targets[0].bindings == {"amount": 10}


def test_a_target_without_an_instruction_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    path.write_text(
        json.dumps({"corpus_path": "x.jsonl", "targets": [{"program": "pumpfun"}]}),
        encoding="utf-8",
    )

    with pytest.raises(WatchError, match="missing"):
        load_plan(path)


def test_a_plan_with_no_corpus_path_is_rejected(tmp_path: Path) -> None:
    """Without somewhere to accumulate, there is no series and drift can never be
    confirmed — so this fails loudly rather than watching into a void."""
    path = tmp_path / "watch.json"
    path.write_text(
        json.dumps({"targets": [{"program": "ore", "instruction": "claim"}]}),
        encoding="utf-8",
    )

    with pytest.raises(WatchError, match="corpus_path"):
        load_plan(path)


def test_a_missing_plan_is_an_honest_error(tmp_path: Path) -> None:
    with pytest.raises(WatchError, match="cannot read"):
        load_plan(tmp_path / "nope.json")


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #
def test_a_clean_pass_says_so(tmp_path: Path) -> None:
    run = run_once(_plan(tmp_path), simulator=lambda *_: _receipt(), now=1_000.0)

    lines = list(format_run(run))

    assert "2/2 targets simulated" in lines[0]
    assert any("no confirmed drift" in line for line in lines)


def test_a_reverting_target_shows_its_class_not_just_a_failure(tmp_path: Path) -> None:
    run = run_once(
        _plan(tmp_path),
        simulator=lambda *_: _receipt(
            status="fail", revert_class="account_error", units=0
        ),
        now=1_000.0,
    )

    assert any("account_error" in line for line in format_run(run))


# --------------------------------------------------------------------------- #
# The break that never reaches the simulator.
# --------------------------------------------------------------------------- #
def test_a_call_that_cannot_be_assembled_is_confirmed_across_passes(
    tmp_path: Path,
) -> None:
    """Found by running the watch for real: a Meteora target whose pool no longer exists
    fails at PLAN time, so it records no series row and detect_drift can never see it. It
    would otherwise flicker as 'unavailable' forever."""

    def broken(target: WatchTarget, _rpc: str, _call: Any, _record: Any) -> Receipt:
        if target.program == "pumpfun":
            raise RuntimeError("cannot assemble")
        return _receipt()

    ticks = iter([0.0, 12 * HOUR, 24 * HOUR, 36 * HOUR])
    runs = watch(
        _plan(tmp_path),
        simulator=broken,
        clock=lambda: next(ticks),
        sleep=lambda _s: None,
        passes=2,
    )

    assert confirm_unavailable(runs, n_confirm=2) == ["pump buy"]


def test_a_single_bad_pass_is_not_a_confirmed_break(tmp_path: Path) -> None:
    """One failed pass is as likely to be a flaky RPC as a real break."""
    calls = {"n": 0}

    def flaky(target: WatchTarget, _rpc: str, _call: Any, _record: Any) -> Receipt:
        calls["n"] += 1
        if target.program == "pumpfun" and calls["n"] <= 2:
            raise RuntimeError("transient")
        return _receipt()

    ticks = iter([0.0, 12 * HOUR, 24 * HOUR, 36 * HOUR])
    runs = watch(
        _plan(tmp_path),
        simulator=flaky,
        clock=lambda: next(ticks),
        sleep=lambda _s: None,
        passes=2,
    )

    assert confirm_unavailable(runs, n_confirm=2) == []


def test_no_confirmation_before_enough_passes(tmp_path: Path) -> None:
    run = run_once(
        _plan(tmp_path),
        simulator=lambda *_: (_ for _ in ()).throw(RuntimeError("x")),
        now=0.0,
    )

    assert confirm_unavailable([run], n_confirm=2) == []
