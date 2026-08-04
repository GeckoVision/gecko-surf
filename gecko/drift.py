"""The drift series — the compounding, honest V2 asset (D2 §5).

A single simulation proves nothing (simulate-clean != prod-correct). The value is the
SERIES: *recipe H simulated clean at slot S, then reverts at slot S′*. This module reads
the categorical ``SimulatedOutcome`` rows, groups them by ``recipe_hash``, and detects a
categorical outcome CHANGE across slots — **N-confirmed** so a single non-deterministic
divergent slot is treated as noise, not drift.

Output is categorical ONLY — a ``(status, revert_class)`` class pair and public slots — so
a ``DriftEvent`` is safe to surface in a provider drift-watch report. No value, pubkey,
amount, or log line can appear here: the input rows already carry none (see
``gecko.corpus.SimulatedOutcome``), and this module only reads their closed-set fields.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .corpus import SimulatedOutcome

__all__ = ["DriftEvent", "detect_drift"]


@dataclass(frozen=True)
class DriftEvent:
    """One confirmed categorical outcome change on a stable ``recipe_hash``.

    ``from_class`` / ``to_class`` are the prior-stable and new-stable ``(status,
    revert_class)`` pairs rendered as ``"status:revert_class"`` (categorical, values-free).
    ``confirmations`` is the number of DISTINCT slots the new class held (``>= n_confirm``).
    """

    recipe_hash: str
    program_id: str
    instruction: str
    from_class: str
    to_class: str
    first_seen_slot: int | None
    confirmed_slot: int | None
    confirmations: int


# The drift "class" is the pair (status, revert_class): a pass->fail flip AND a fail-class
# change (slippage -> account_error) are both drift.
_Class = tuple[str, str]


def _class_of(row: SimulatedOutcome) -> _Class:
    return (row.status, row.revert_class)


def _fmt(cls: _Class) -> str:
    return f"{cls[0]}:{cls[1]}"


def _slot_key(row: SimulatedOutcome) -> tuple[bool, int]:
    """Order by slot; a ``None`` slot sorts LAST (it cannot anchor the series)."""
    return (row.slot is None, row.slot if row.slot is not None else 0)


def _detect_group(ordered: list[SimulatedOutcome], n_confirm: int) -> list[DriftEvent]:
    """Detect confirmed class changes within one ``recipe_hash`` group, slot-ordered.

    Walks the series holding a ``baseline`` (the current stable class). A differing class
    becomes a *candidate*; it is only promoted to a ``DriftEvent`` once it is observed on
    ``>= n_confirm`` DISTINCT slots. A baseline reassertion (or a switch to a third class)
    discards an unconfirmed candidate — a lone divergent slot is noise, not drift. On
    confirmation the baseline shifts, so a later re-flip is detected too.
    """
    events: list[DriftEvent] = []
    if not ordered:
        return events

    baseline = _class_of(ordered[0])
    program_id = ordered[0].program_id
    instruction = ordered[0].instruction

    cand_class: _Class | None = None
    cand_slots: set[int | None] = set()
    cand_first_slot: int | None = None

    for row in ordered:
        cls = _class_of(row)
        if cls == baseline:
            cand_class, cand_slots, cand_first_slot = None, set(), None
            continue
        if cls != cand_class:
            cand_class = cls
            cand_slots = {row.slot}
            cand_first_slot = row.slot
        else:
            cand_slots.add(row.slot)
        if len(cand_slots) >= n_confirm:
            events.append(
                DriftEvent(
                    recipe_hash=row.recipe_hash,
                    program_id=program_id,
                    instruction=instruction,
                    from_class=_fmt(baseline),
                    to_class=_fmt(cand_class),
                    first_seen_slot=cand_first_slot,
                    confirmed_slot=row.slot,
                    confirmations=len(cand_slots),
                )
            )
            baseline = cand_class
            cand_class, cand_slots, cand_first_slot = None, set(), None
    return events


def detect_drift(
    rows: Iterable[SimulatedOutcome], *, n_confirm: int = 2
) -> list[DriftEvent]:
    """Group ``rows`` by ``recipe_hash`` and emit an N-confirmed ``DriftEvent`` per stable
    class change (§5). A single divergent slot is noise, not drift.

    ``n_confirm`` is the number of DISTINCT slots the new class must hold before it counts.
    Ordering within a group is by slot. Categorical output only — safe to surface.
    """
    groups: dict[str, list[SimulatedOutcome]] = defaultdict(list)
    for row in rows:
        groups[row.recipe_hash].append(row)

    events: list[DriftEvent] = []
    for group in groups.values():
        ordered = sorted(group, key=_slot_key)
        events.extend(_detect_group(ordered, n_confirm))
    return events
