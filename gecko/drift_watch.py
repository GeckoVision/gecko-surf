"""The drift scheduler — re-simulate a known-good plan until it stops being good.

``gecko.drift`` answers "did the outcome class change" for rows that already exist.
Nothing produced those rows on a schedule, so drift detection was opt-in evidence: real,
but only if a human remembered to run it. That is the difference between a detector and
CI, and it is the whole gap this module closes.

A **watch plan** is a list of calls a team depends on. Each pass re-simulates every target
against a fork, appends one categorical row per target to the corpus, and re-runs the
N-confirmed detector over the accumulated series. When a program upgrade breaks a call
that used to pass, the change is confirmed across distinct slots and surfaces as a
``DriftEvent`` — without anyone asking.

**Scheduling is a pure function** (:func:`due`), and the simulator is an injected seam, so
the entire scheduler is falsifiable offline with no clock, no network, and no RPC. The
loop in :func:`watch` is thin transport around it.

Control plane, unchanged. The watch plan is **local config the user authored** — it holds
the bindings needed to re-run a call and is never transmitted, never recorded, and never
part of the corpus. What gets persisted is what has always been persisted: a categorical
``SimulatedOutcome`` (status / revert class / units / slot / network + a values-free
structural ``recipe_hash``). No pubkey, no amount, no log line crosses into a stored row.

Nothing here signs or broadcasts. Every pass is ``simulateTransaction`` on an unsigned
transaction, the same $0 path the demos use.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .corpus import SimulatedOutcome, simulated_outcome_from_record
from .drift import DriftEvent, detect_drift
from .rpc import LOCAL_RPC, RpcCall
from .simulate import Receipt

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "TargetResult",
    "WatchError",
    "WatchPlan",
    "WatchRun",
    "WatchTarget",
    "confirm_unavailable",
    "due",
    "load_plan",
    "run_once",
    "format_run",
    "watch",
]

#: Twelve hours. A program upgrade is a rare event and every pass costs an RPC round trip
#: against a fork, so the default is deliberately unhurried — a scheduler that hammers the
#: chain to re-prove a call that changes twice a year is a cost, not a feature. Teams that
#: need tighter bounds set it explicitly.
DEFAULT_INTERVAL_SECONDS = 12 * 60 * 60


class WatchError(Exception):
    """A watch plan could not be read or run. Never carries a secret."""


@dataclass(frozen=True)
class WatchTarget:
    """One call a team depends on, expressed the way they would make it.

    ``bindings`` are the same mapping the landing orchestrators take. They live in the
    local plan file only — they are what lets us re-run the call, and they never reach a
    persisted row.
    """

    label: str
    program: str
    instruction: str
    bindings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchPlan:
    """What to re-simulate, where to accumulate it, and how often."""

    targets: tuple[WatchTarget, ...]
    corpus_path: Path
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    n_confirm: int = 2
    rpc_url: str = LOCAL_RPC


@dataclass(frozen=True)
class TargetResult:
    """What one target did on one pass. ``receipt`` is None when the target could not be
    run at all — an honest gap, never a silent skip."""

    target: WatchTarget
    receipt: Receipt | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.receipt is not None


@dataclass(frozen=True)
class WatchRun:
    """One pass: what each target did, and any drift the accumulated series now confirms."""

    ran_at: int
    results: tuple[TargetResult, ...]
    events: tuple[DriftEvent, ...]

    @property
    def failed(self) -> tuple[TargetResult, ...]:
        return tuple(r for r in self.results if not r.ok)


# --------------------------------------------------------------------------- #
# Scheduling — a pure decision, so the schedule is testable without a clock.
# --------------------------------------------------------------------------- #
def due(now: float, last_run: float | None, interval_seconds: int) -> bool:
    """Should a pass run at ``now``?

    ``last_run is None`` means we have never run, so the first pass is always due — a
    scheduler that waits a full interval before its first observation has no baseline to
    detect drift against.

    A ``last_run`` in the future (a clock that moved backwards, a plan copied between
    machines) is treated as due rather than blocking the watch until the clock catches
    up: a spurious extra simulation is cheap, a silently stalled watch is not.
    """
    if last_run is None:
        return True
    if last_run > now:
        return True
    return (now - last_run) >= interval_seconds


# --------------------------------------------------------------------------- #
# The plan file — local config, never transmitted.
# --------------------------------------------------------------------------- #
def load_plan(path: str | Path) -> WatchPlan:
    """Read a watch plan from JSON.

    Shape::

        {
          "corpus_path": "~/.gecko/corpus.jsonl",
          "interval_seconds": 43200,
          "n_confirm": 2,
          "rpc_url": "http://127.0.0.1:8899",
          "targets": [
            {"label": "pump buy", "program": "pumpfun", "instruction": "buy",
             "bindings": {"mint": "...", "user": "...", "amount": 1000000}}
          ]
        }
    """
    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WatchError(f"cannot read watch plan at {source}") from exc
    except json.JSONDecodeError as exc:
        raise WatchError(f"watch plan at {source} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise WatchError("watch plan must be a JSON object")

    entries = raw.get("targets")
    if not isinstance(entries, list) or not entries:
        raise WatchError("watch plan has no targets")

    targets: list[WatchTarget] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise WatchError("every target must be an object")
        missing = [k for k in ("program", "instruction") if not entry.get(k)]
        if missing:
            raise WatchError(f"target is missing {missing}")
        targets.append(
            WatchTarget(
                label=str(
                    entry.get("label") or f"{entry['program']} {entry['instruction']}"
                ),
                program=str(entry["program"]),
                instruction=str(entry["instruction"]),
                bindings=dict(entry.get("bindings") or {}),
            )
        )

    corpus_path = raw.get("corpus_path")
    if not corpus_path:
        raise WatchError("watch plan needs a corpus_path to accumulate the series")

    return WatchPlan(
        targets=tuple(targets),
        corpus_path=Path(str(corpus_path)).expanduser(),
        interval_seconds=int(raw.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS),
        n_confirm=int(raw.get("n_confirm") or 2),
        rpc_url=str(raw.get("rpc_url") or LOCAL_RPC),
    )


# --------------------------------------------------------------------------- #
# The simulator seam — injected, so a pass is falsifiable with no RPC.
# --------------------------------------------------------------------------- #
Simulator = Callable[[WatchTarget, str, RpcCall | None, "str | Path"], Receipt]


def _default_simulator(
    target: WatchTarget,
    rpc_url: str,
    rpc_call: RpcCall | None,
    record_to: str | Path,
) -> Receipt:
    """Dispatch a target to the landing orchestrator that owns it.

    Each orchestrator already records its own categorical row when ``record_to`` is set,
    so the scheduler never writes to the corpus itself — one writer, one contract.
    """
    from .providers import (  # local import: keeps solders off the import path until used
        jupiter_landing,
        metadao_landing,
        meteora_landing,
        ore_landing,
        pumpfun_landing,
    )

    common: dict[str, Any] = {
        "rpc_url": rpc_url,
        "rpc_call": rpc_call,
        "record_to": record_to,
        "include_derive_only": False,  # the baseline is the story; the watch is the plan
    }
    key = (target.program.lower(), target.instruction.lower())
    if key == ("pumpfun", "buy"):
        return pumpfun_landing.simulate_buy_landing(
            target.bindings, **common
        ).landing_receipt
    if key == ("pumpfun", "sell"):
        return pumpfun_landing.simulate_sell_landing(
            target.bindings, **common
        ).landing_receipt
    if key == ("meteora", "swap"):
        return meteora_landing.simulate_swap_landing(
            target.bindings, **common
        ).landing_receipt
    if key == ("ore", "claim"):
        return ore_landing.simulate_claim_landing(
            target.bindings, **common
        ).landing_receipt
    if key == ("metadao", "fund"):
        return metadao_landing.simulate_fund_landing(
            target.bindings, **common
        ).landing_receipt
    if key == ("jupiter", "route"):
        return jupiter_landing.simulate_route_landing(
            target.bindings, **common
        ).landing_receipt
    raise WatchError(f"no orchestrator for {target.program}/{target.instruction}")


def _read_series(corpus_path: Path) -> list[SimulatedOutcome]:
    """The accumulated simulated series, best-effort.

    A malformed line is skipped rather than failing the pass: the series is append-only
    evidence, and one bad append must not blind the watch to every row around it.
    """
    from .corpus import simulated_sibling

    target = simulated_sibling(corpus_path)
    if not target.exists():
        return []
    rows: list[SimulatedOutcome] = []
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(simulated_outcome_from_record(json.loads(line)))
            except Exception:  # noqa: BLE001 - one corrupt append never blinds the watch
                continue
    return rows


def run_once(
    plan: WatchPlan,
    *,
    rpc_call: RpcCall | None = None,
    simulator: Simulator | None = None,
    now: float | None = None,
) -> WatchRun:
    """Re-simulate every target once, then re-detect drift over the whole series.

    A target that cannot be run is reported as a failed :class:`TargetResult` and the pass
    continues — one broken target must not stop the watch on the others, and a silent skip
    would look identical to a passing call.
    """
    ran_at = int(now if now is not None else time.time())
    run = simulator or _default_simulator

    results: list[TargetResult] = []
    for target in plan.targets:
        try:
            receipt = run(target, plan.rpc_url, rpc_call, plan.corpus_path)
            results.append(TargetResult(target=target, receipt=receipt))
        except Exception as exc:  # noqa: BLE001 - keep the pass alive, report honestly
            results.append(
                TargetResult(target=target, receipt=None, error=type(exc).__name__)
            )

    events = detect_drift(_read_series(plan.corpus_path), n_confirm=plan.n_confirm)
    return WatchRun(ran_at=ran_at, results=tuple(results), events=tuple(events))


def watch(
    plan: WatchPlan,
    *,
    rpc_call: RpcCall | None = None,
    simulator: Simulator | None = None,
    on_run: Callable[[WatchRun], None] | None = None,
    passes: int | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> list[WatchRun]:
    """Run passes on the plan's interval until ``passes`` are done (``None`` = forever).

    ``clock`` and ``sleep`` are injected so the loop runs instantly in a test — the
    scheduling behaviour is proven without waiting twelve hours for it.
    """
    runs: list[WatchRun] = []
    last_run: float | None = None
    while passes is None or len(runs) < passes:
        moment = clock()
        if due(moment, last_run, plan.interval_seconds):
            run = run_once(plan, rpc_call=rpc_call, simulator=simulator, now=moment)
            last_run = moment
            runs.append(run)
            if on_run is not None:
                on_run(run)
            if passes is not None and len(runs) >= passes:
                break
        sleep(min(plan.interval_seconds, 60))
    return runs


def confirm_unavailable(runs: Sequence[WatchRun], n_confirm: int = 2) -> list[str]:
    """Targets that have been unrunnable for the last ``n_confirm`` consecutive passes.

    A call can break BEFORE it reaches the simulator — a program upgrade changes an
    account layout and the plan can no longer be assembled at all. That break records no
    series row (there is no outcome to record), so ``detect_drift`` is structurally blind
    to it and it would otherwise flicker as a transient "unavailable" line forever.

    It is also the break a team most wants to hear about: a call they depend on can no
    longer even be constructed. So the watch confirms it the same way drift is confirmed —
    repetition across passes, never a single bad pass, since one failed pass is as likely
    to be a flaky RPC as a real break.

    Pure: a function of the runs it is handed, with no clock and no state of its own.
    """
    if n_confirm <= 0 or len(runs) < n_confirm:
        return []
    recent = runs[-n_confirm:]
    labels = [r.target.label for r in recent[0].results if not r.ok]
    confirmed = [
        label
        for label in labels
        if all(
            any(res.target.label == label and not res.ok for res in run.results)
            for run in recent[1:]
        )
    ]
    return confirmed


def format_run(run: WatchRun) -> Iterable[str]:
    """Human lines for a pass — the CLI's whole rendering, kept out of the CLI."""
    ok = [r for r in run.results if r.ok]
    yield f"pass at {run.ran_at}: {len(ok)}/{len(run.results)} targets simulated"
    for result in run.results:
        if result.receipt is None:
            yield f"  ?   {result.target.label}  unavailable ({result.error})"
        else:
            units = result.receipt.units_consumed
            mark = (
                "ok" if result.receipt.status == "pass" else result.receipt.revert_class
            )
            yield f"  {'ok' if result.receipt.status == 'pass' else '!!'}  {result.target.label}  {mark}  {units:,} CU"
    if not run.events:
        yield "  no confirmed drift"
        return
    for event in run.events:
        yield (
            f"  DRIFT  {event.program_id[:8]}… {event.instruction}: "
            f"{event.from_class} -> {event.to_class} "
            f"({event.confirmations} slots)"
        )
