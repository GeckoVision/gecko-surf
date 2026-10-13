"""Who a relay rebate is credited to, once, and when to remind the founder to sweep.

Founder ruling, 8 Oct: ``rebate-address`` = the Kora relay's fee-payer address
(:attr:`gecko.anyquery.config.AnyQueryConfig.relay_fee_payer`), so backrun rebates
refill the wallet that pays users' network fees. The user's share is a discount on their
next run, written in their own ledger.

**Credited at most once across ALL customers (review finding 5).** The ledger's
``one_entry_per_signature`` index lives inside each customer's database, so two
customers with a payment in the same slot could each be credited the same rebate. The
:class:`RebateIndex` is one collection OUTSIDE every per-customer database, unique on
the rebate's signature. A row holds only public chain data: the rebate's signature, its
slot, the lamports the relay gained, and the signature of the payment it was matched to
(``None`` = unattributed). No customer id, run id or amount owed, so it pools nothing a
customer could ask to delete; deleting a customer leaves rows that name only public
transactions.

**Matching.** An inflow to the relay is credited only when it lands in the same slot as
a known Gecko payment from the customer being observed; any other inflow is recorded
unattributed. An unattributed row may later be claimed by exactly one payment (another
customer's pass may be the one that knows the payment), and a claimed row never changes
hands. A claim is idempotent for the SAME payment, so a crash between the claim and the
ledger write is retried, never lost and never doubled.

**The hot balance.** :func:`check_relay_balance` only logs: below
``relay_low_below_lamports`` a top-up reminder, above ``relay_sweep_above_lamports`` a
sweep reminder. The founder moves the money by hand; nothing here can sign or send.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .config import AnyQueryConfig

__all__ = [
    "REBATE_INDEX_COLLECTION",
    "REBATE_INDEX_DB",
    "MemoryRebateIndex",
    "MongoRebateIndex",
    "RebateIndex",
    "RebateIndexError",
    "RebateRow",
    "RelayBalance",
    "check_relay_balance",
]

logger = logging.getLogger(__name__)

#: Not ``gq_``-prefixed: it is not a customer's database and ``delete_customer`` never
#: drops it.
REBATE_INDEX_DB = "gecko_rebate_index"
REBATE_INDEX_COLLECTION = "rebates"
_DUPLICATE = 11000

RelayBalanceStatus = Literal["low", "ok", "sweep"]


class RebateIndexError(Exception):
    """The index refused or could not answer. Never carries a connection string."""


@dataclass(frozen=True)
class RebateRow:
    signature: str
    slot: int
    lamports: int
    #: The payment's on-chain signature, or ``None`` while unattributed.
    payment_signature: str | None


class RebateIndex(Protocol):
    def row(self, signature: str) -> RebateRow | None: ...

    def claim(
        self, signature: str, *, slot: int, lamports: int, payment_signature: str
    ) -> bool:
        """True when this rebate is (now, or already) credited to ``payment_signature``;
        False when another payment holds it."""
        ...

    def record_unattributed(self, signature: str, *, slot: int, lamports: int) -> None:
        """Note an inflow no known payment matched. Never removes an attribution."""
        ...


def _check(signature: str, slot: int, lamports: int) -> None:
    if not isinstance(signature, str) or not signature:
        raise RebateIndexError("a rebate row needs its signature")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise RebateIndexError("a rebate row needs its slot")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0:
        raise RebateIndexError("a rebate row is an inflow: lamports must be positive")


def _same_facts(held: RebateRow, slot: int, lamports: int) -> None:
    # The chain does not change a landed transaction; a mismatch is a bug or an attack.
    if (held.slot, held.lamports) != (slot, lamports):
        raise RebateIndexError(
            f"rebate {held.signature} is already indexed with other slot or lamports"
        )


@dataclass
class MemoryRebateIndex:
    """One process's index: tests and recorded runs. Production injects Mongo's, or the
    at-most-once guarantee holds only inside one process."""

    rows: dict[str, RebateRow] = field(default_factory=dict)

    def row(self, signature: str) -> RebateRow | None:
        return self.rows.get(signature)

    def claim(
        self, signature: str, *, slot: int, lamports: int, payment_signature: str
    ) -> bool:
        _check(signature, slot, lamports)
        if not payment_signature:
            raise RebateIndexError("a claim names the payment it was matched to")
        held = self.rows.get(signature)
        if held is not None:
            _same_facts(held, slot, lamports)
            if held.payment_signature is not None:
                return held.payment_signature == payment_signature
        self.rows[signature] = RebateRow(signature, slot, lamports, payment_signature)
        return True

    def record_unattributed(self, signature: str, *, slot: int, lamports: int) -> None:
        _check(signature, slot, lamports)
        held = self.rows.get(signature)
        if held is not None:
            _same_facts(held, slot, lamports)
            return
        self.rows[signature] = RebateRow(signature, slot, lamports, None)


def _driver_errors() -> tuple[type[BaseException], ...]:
    try:
        from pymongo.errors import PyMongoError
    except ImportError:
        return ()
    return (PyMongoError,)


def _is_duplicate(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == _DUPLICATE


@dataclass
class MongoRebateIndex:
    """The index on a pymongo-shaped client, in :data:`REBATE_INDEX_DB`. ``_id`` is the
    rebate's signature, so the server itself refuses a second row for one rebate."""

    client: Any
    db_name: str = REBATE_INDEX_DB
    #: Extra exception types read as "the index could not answer" (tests inject one).
    unavailable: tuple[type[BaseException], ...] = ()

    @property
    def _rows(self) -> Any:
        return self.client[self.db_name][REBATE_INDEX_COLLECTION]

    @contextmanager
    def _guard(self) -> Iterator[None]:
        errors = (*_driver_errors(), *self.unavailable)
        try:
            yield
        except RebateIndexError:
            raise
        except Exception as exc:
            if _is_duplicate(exc):
                raise
            if errors and isinstance(exc, errors):
                # The driver's message can carry the host; ours never does.
                raise RebateIndexError(
                    "the rebate index did not answer; nothing was credited"
                ) from None
            raise

    def row(self, signature: str) -> RebateRow | None:
        with self._guard():
            doc = self._rows.find_one({"_id": signature})
        if doc is None:
            return None
        return RebateRow(
            str(doc["signature"]),
            int(doc["slot"]),
            int(doc["lamports"]),
            doc.get("payment_signature"),
        )

    def _insert(
        self, signature: str, slot: int, lamports: int, payment: str | None
    ) -> bool:
        """True when inserted, False when a row for this signature already exists."""
        doc = {
            "_id": signature,
            "signature": signature,
            "slot": slot,
            "lamports": lamports,
            "payment_signature": payment,
        }
        try:
            with self._guard():
                self._rows.insert_one(doc)
        except Exception as exc:
            if not _is_duplicate(exc):
                raise
            return False
        return True

    def claim(
        self, signature: str, *, slot: int, lamports: int, payment_signature: str
    ) -> bool:
        _check(signature, slot, lamports)
        if not payment_signature:
            raise RebateIndexError("a claim names the payment it was matched to")
        if self._insert(signature, slot, lamports, payment_signature):
            return True
        held = self.row(signature)
        if held is None:  # pragma: no cover - rows are never deleted
            raise RebateIndexError(f"rebate {signature} vanished during the claim")
        _same_facts(held, slot, lamports)
        if held.payment_signature is not None:
            return held.payment_signature == payment_signature
        # Unattributed: the filter on ``payment_signature: None`` makes the move atomic,
        # so two concurrent claims cannot both match.
        with self._guard():
            result = self._rows.update_one(
                {"_id": signature, "payment_signature": None},
                {"$set": {"payment_signature": payment_signature}},
            )
        if getattr(result, "matched_count", 0) == 1:
            return True
        won = self.row(signature)
        return won is not None and won.payment_signature == payment_signature

    def record_unattributed(self, signature: str, *, slot: int, lamports: int) -> None:
        _check(signature, slot, lamports)
        if self._insert(signature, slot, lamports, None):
            return
        held = self.row(signature)
        if held is not None:
            _same_facts(held, slot, lamports)


@dataclass(frozen=True)
class RelayBalance:
    lamports: int
    status: RelayBalanceStatus
    reason: str


def check_relay_balance(lamports: int, config: AnyQueryConfig) -> RelayBalance:
    """Compare the relay's balance with the founder's two thresholds and log a reminder
    when it is outside them. Reads nothing and moves nothing."""
    low = config.relay_low_below_lamports
    ceiling = config.relay_sweep_above_lamports
    if lamports > ceiling:
        reason = (
            f"relay hot balance {lamports / 1e9:.4f} SOL is above the "
            f"{ceiling / 1e9:.4f} SOL ceiling: sweep the surplus to cold by hand"
        )
        logger.warning("%s", reason)
        return RelayBalance(lamports, "sweep", reason)
    if lamports < low:
        reason = (
            f"relay hot balance {lamports / 1e9:.4f} SOL is below "
            f"{low / 1e9:.4f} SOL: top up the relay by hand"
        )
        logger.warning("%s", reason)
        return RelayBalance(lamports, "low", reason)
    reason = f"relay hot balance {lamports / 1e9:.4f} SOL is within the thresholds"
    logger.info("%s", reason)
    return RelayBalance(lamports, "ok", reason)
