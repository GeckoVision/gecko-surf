"""Self-repair, shown at $0: two recorded runs, the second with a changed upstream shape.

Run 1 is the made-up watch as usual: it leaves a field-ledger fingerprint and a stored
snapshot. Run 2 flips the fixture's ``changed_shape`` flag for one upstream (crustdata
by default), so the entity field's first source comes back without the path its
fingerprint carried. The run says so, walks the source map and prints one line per
field it repaired, e.g.::

    entity: crustdata drifted (field path missing) → filled from predictleads, agrees
    with last snapshot

Everything is made up (``.example`` companies), in memory or in a temporary folder.
Nothing is called and nothing is spent.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .deps import supplier_deps_for
from .fixture import SAMPLE_SUPPLIERS_CSV, RecordedSupplierOrthogonal
from .history import changes_since
from .job import WatchResult, run_watch
from .store import MemoryFieldLedger, read_runs

SHEET = "suppliers.csv"
#: 2026-10-08 12:00 UTC, then an hour later: two distinct run ids.
_T0 = dt.datetime(2026, 10, 8, 12, tzinfo=dt.timezone.utc).timestamp()


def _fixed(at: float) -> Callable[[], float]:
    return lambda: at


@dataclass
class SelfRepairDemo:
    first: WatchResult
    second: WatchResult
    changes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"run {self.first.run_id}: recorded, normal shapes "
            f"(fields that failed over: {self.first.receipt['failovers']})",
            f"run {self.second.run_id}: recorded, crustdata replies in a changed shape",
        ]
        out += [f"  {line}" for line in self.second.repairs]
        out.append(f"changes since {self.first.run_id}:")
        out += [f"  {line}" for line in self.changes] or ["  none"]
        return out


def run_self_repair_demo(
    folder: Path | None = None, *, changed: frozenset[str] = frozenset({"crustdata"})
) -> SelfRepairDemo:
    """In ``folder`` when given (kept), else in a temporary folder (removed)."""
    if folder is not None:
        return _demo(folder, changed)
    with tempfile.TemporaryDirectory(prefix="gecko-self-repair-") as tmp:
        return _demo(Path(tmp), changed)


def _demo(folder: Path, changed: frozenset[str]) -> SelfRepairDemo:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / SHEET).write_text(SAMPLE_SUPPLIERS_CSV, encoding="utf-8")
    ledger = MemoryFieldLedger()  # shared: run 2 reads run 1's fingerprints
    today = dt.date(2026, 10, 8)
    results = []
    for offset, shape in ((0, frozenset[str]()), (3600, changed)):
        deps = supplier_deps_for(
            "recorded",
            folder=folder,
            field_ledger=ledger,
            transport=RecordedSupplierOrthogonal(today=today, changed_shape=shape),
            clock=_fixed(_T0 + offset),
        )
        results.append(run_watch(deps, SHEET, confirm=True))
    first, second = results
    diff = changes_since(read_runs(folder), first.run_id)
    lines = [
        f"{c.supplier} / {c.field}: {c.change} ({', '.join(c.what)})"
        + (
            f"; source {c.source_before} -> {c.source_after}"
            if c.source_changed
            else ""
        )
        for c in diff.changes
    ]
    return SelfRepairDemo(first, second, lines)
