"""The host-wide upstream spend cap, as one Mongo document every task shares.

What it counts is GECKO's spend on its own Orthogonal key, all customers together: one
integer, ``total_cents``. It holds no customer id, run id, request id or payload, so it
is not customer data and lives in :data:`UPSTREAM_SPEND_DB`, outside every ``gq_``
customer database; ``delete_customer`` never touches it.

**Why Mongo and not a file.** The Fargate task has no persistent disk, so a file ledger
started at zero on every deploy and the cap bound one task's lifetime, not the host's.
**Why a reservation.** Two runs that read the total and then add to it can both read
"room left"; :meth:`MongoSpendLedger.reserve` is one ``findOneAndUpdate`` whose filter
is ``total_cents <= cap - cents`` and whose update is ``$inc``, so the server applies the
check and the increment as one step. :class:`gecko.orthogonal.spend.SpendGuard` then
settles the hold to what the upstream charged.

Fails closed: an unreachable server or an unreadable total refuses the call
(:class:`SpendCapError`), with a message that never carries the connection string.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..orthogonal.errors import SpendCapError

__all__ = [
    "UPSTREAM_SPEND_COLLECTION",
    "UPSTREAM_SPEND_DB",
    "UPSTREAM_SPEND_KEY",
    "MongoSpendLedger",
]

#: Not ``gq_``-prefixed: Gecko's own spend, not a customer's database.
UPSTREAM_SPEND_DB = "gecko_upstream_spend"
UPSTREAM_SPEND_COLLECTION = "totals"
#: One document per upstream key Gecko pays with.
UPSTREAM_SPEND_KEY = "orthogonal"
_DUPLICATE = 11000


def _driver_errors() -> tuple[type[BaseException], ...]:
    try:
        from pymongo.errors import PyMongoError
    except ImportError:
        return ()
    return (PyMongoError,)


@dataclass
class MongoSpendLedger:
    """A :class:`gecko.orthogonal.spend.ReservingLedger` on a pymongo-shaped client."""

    client: Any
    key: str = UPSTREAM_SPEND_KEY
    db_name: str = UPSTREAM_SPEND_DB
    #: Extra exception types read as "the ledger could not answer" (tests inject one).
    unavailable: tuple[type[BaseException], ...] = ()
    _row_exists: bool = field(default=False, repr=False)

    @property
    def _rows(self) -> Any:
        return self.client[self.db_name][UPSTREAM_SPEND_COLLECTION]

    @contextmanager
    def _guard(self) -> Iterator[None]:
        errors = (*_driver_errors(), *self.unavailable)
        try:
            yield
        except SpendCapError:
            raise
        except Exception as exc:
            if errors and isinstance(exc, errors):
                # The driver's message can carry the host; ours never does.
                raise SpendCapError(
                    "The upstream spend ledger did not answer. Nothing was spent."
                ) from None
            raise

    def _ensure_row(self) -> None:
        """Create the zero row once, so ``reserve`` never needs an upsert: an upsert
        whose ``$lte`` filter misses on a FULL row would try to insert a duplicate."""
        if self._row_exists:
            return
        with self._guard():
            try:
                self._rows.insert_one({"_id": self.key, "total_cents": 0})
            except Exception as exc:
                # The duplicate is read INSIDE the guard: pymongo's DuplicateKeyError
                # is a PyMongoError, so outside it the guard turned "the row already
                # exists" into "the ledger did not answer" and refused every first
                # reservation of a new ledger object (live, 9 Oct: 0 of 50, refused).
                if getattr(exc, "code", None) != _DUPLICATE:
                    raise
        self._row_exists = True

    def total_cents(self) -> int:
        with self._guard():
            doc = self._rows.find_one({"_id": self.key})
        if doc is None:
            return 0
        total = doc.get("total_cents")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            # An unreadable total must not read as "nothing spent yet".
            raise SpendCapError(
                "The upstream spend ledger is unreadable. Nothing was spent. Check the "
                "Orthogonal balance, then fix the total by hand."
            )
        return total

    def add(self, cents: int) -> None:
        self._inc(cents)

    def reserve(self, cents: int, cap: int) -> bool:
        if cents < 0 or cents > cap:
            return False
        self._ensure_row()
        with self._guard():
            before = self._rows.find_one_and_update(
                {"_id": self.key, "total_cents": {"$lte": cap - cents}},
                {"$inc": {"total_cents": cents}},
            )
        return before is not None

    def settle(self, held: int, charged: int) -> None:
        if charged != held:
            self._inc(charged - held)

    def _inc(self, cents: int) -> None:
        # Unconditional: a charge already happened upstream, so it is recorded even
        # when it takes the total past the cap.
        with self._guard():
            self._rows.find_one_and_update(
                {"_id": self.key}, {"$inc": {"total_cents": cents}}, upsert=True
            )
        self._row_exists = True
