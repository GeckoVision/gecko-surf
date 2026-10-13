"""Our spend cap, in cents, counted from what Orthogonal says it CHARGED.

Orthogonal enforces no cap of its own (its only hard limit is the account balance), so
this is the cap. Two limits: per run, and a persistent total across the whole rehearsal.
Before every call the guard asks "would one more full-price call cross a limit?"; after
it, the returned ``priceCents`` (charged — not ``quotedPriceCents``, which stays at the
list price even on free continuation pages) is added to both.

A ledger shared by concurrent runs (:class:`ReservingLedger`, e.g. the hosted Mongo
one) cannot be read-then-added: two runs would both read "room left" and both spend.
For those the guard RESERVES the worst case in one conditional increment before the
call, then settles the hold to what was charged (or releases it if the call failed).
The file and memory ledgers keep the read-then-add path; they have one writer.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import SpendCapError

PER_RUN_CAP_CENTS = 10
REHEARSAL_CAP_CENTS = 100


def default_ledger_path() -> Path:
    return Path.home() / ".gecko" / "orthogonal" / "ledger.json"


class Ledger(Protocol):
    def total_cents(self) -> int: ...

    def add(self, cents: int) -> None: ...


@runtime_checkable
class ReservingLedger(Protocol):
    """A ledger more than one process writes to, so the cap check must be atomic."""

    def total_cents(self) -> int: ...

    def add(self, cents: int) -> None: ...

    def reserve(self, cents: int, cap: int) -> bool:
        """Add ``cents`` only if the total stays at or under ``cap``, in one atomic
        step. False means refused, and nothing was added."""
        ...

    def settle(self, held: int, charged: int) -> None:
        """Replace a hold of ``held`` with the ``charged`` amount (0 releases it)."""
        ...


@dataclass
class MemoryLedger:
    """For recorded runs and tests: fake charges never touch the real ledger."""

    total: int = 0

    def total_cents(self) -> int:
        return self.total

    def add(self, cents: int) -> None:
        self.total += cents


@dataclass
class FileLedger:
    """A single number on disk: cents charged so far. No request ids, no names."""

    path: Path = field(default_factory=default_ledger_path)

    def total_cents(self) -> int:
        if not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            total = int(raw["total_cents"])
        except (ValueError, KeyError, TypeError, OSError):
            # Fail closed: an unreadable ledger must not read as "nothing spent yet".
            raise SpendCapError(
                f"The spend ledger at {self.path} is unreadable. Nothing was spent. "
                "Fix or delete it by hand after checking the Orthogonal balance."
            ) from None
        if total < 0:
            raise SpendCapError(f"The spend ledger at {self.path} is negative.")
        return total

    def add(self, cents: int) -> None:
        total = self.total_cents() + cents
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"total_cents": total}, handle)
        os.replace(tmp, self.path)


@dataclass
class SpendGuard:
    ledger: Ledger
    per_run_cap: int = PER_RUN_CAP_CENTS
    total_cap: int = REHEARSAL_CAP_CENTS
    spent: int = 0
    #: Cents reserved on a :class:`ReservingLedger` and not yet settled.
    held: int = 0

    def check_before(self, bound_cents: int) -> None:
        """Refuse a call whose worst case would cross either limit. On a
        :class:`ReservingLedger` a pass also HOLDS ``bound_cents`` until :meth:`record`
        or :meth:`release`, so call this only right before the paid call."""
        if not isinstance(self.ledger, ReservingLedger):
            self.check_fits(bound_cents)
            return
        self._check_run(bound_cents)
        if not self.ledger.reserve(bound_cents, self.total_cap):
            self._refuse_total(self.ledger.total_cents())
        self.held += bound_cents

    def check_fits(self, bound_cents: int) -> None:
        """The same refusal as :meth:`check_before`, holding nothing: for planning and
        for "is this source still affordable" probes that may never make the call."""
        self._check_run(bound_cents)
        so_far = self.ledger.total_cents()
        if so_far + bound_cents > self.total_cap:
            self._refuse_total(so_far)

    def _check_run(self, bound_cents: int) -> None:
        if self.spent + bound_cents > self.per_run_cap:
            raise SpendCapError(
                f"Stopped: one more call could cost ${bound_cents / 100:.2f} and this "
                f"run is capped at ${self.per_run_cap / 100:.2f} "
                f"(${self.spent / 100:.2f} spent)."
            )

    def _refuse_total(self, so_far: int) -> None:
        raise SpendCapError(
            f"Stopped: the rehearsal is capped at ${self.total_cap / 100:.2f} and "
            f"${so_far / 100:.2f} is already spent. Nothing more was spent."
        )

    def record(self, charged_cents: int) -> None:
        self.spent += charged_cents
        if self.held and isinstance(self.ledger, ReservingLedger):
            held, self.held = self.held, 0
            self.ledger.settle(held, charged_cents)
            return
        self.ledger.add(charged_cents)

    def release(self) -> None:
        """Drop an unsettled hold: the call it was taken for never returned a reply."""
        if self.held and isinstance(self.ledger, ReservingLedger):
            held, self.held = self.held, 0
            self.ledger.settle(held, 0)
