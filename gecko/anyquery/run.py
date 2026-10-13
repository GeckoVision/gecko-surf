"""The run contract: a validated plan and its confirmed quote go through the Orthogonal
core's ``Runner`` and ``SpendGuard``, capped at the quote. ``execute`` itself is lane 2.

Run ids are ``YYYYMMDD-HHMMSS-<6hex>`` so ``supplier.history``'s lexical sort of run ids
is also time order.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from gecko.orthogonal.job import JobDeps
from gecko.orthogonal.supplier.gleif import PublicFetch

from gecko.orthogonal.supplier.fields import FIELD_STATES

from .store import Store
from .types import CallMode, FactRow, PaymentStatus, QueryPlan, Quote, RunRecord

__all__ = [
    "Execute",
    "RUN_ID_RE",
    "RunDeps",
    "RunRefusedError",
    "RunResult",
    "claim_record",
    "new_run_id",
]

RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


@dataclass
class RunDeps:
    base: JobDeps  # Orthogonal core (transport, session, clock)
    mode: CallMode
    store: Store
    public_fetch: PublicFetch


@dataclass(frozen=True)
class RunResult:
    record: RunRecord
    facts: tuple[FactRow, ...]
    call_rows: tuple[dict[str, Any], ...]


class RunRefusedError(Exception):
    """The run was refused before anything was stored or spent. Our words only."""


class Execute(Protocol):
    """SpendGuard per-run cap = ``quote.upstream_max_cents``; rank-0 source per
    (subject, field), read with ``reader.read_path``, the repair walk on drift or
    failure using the store's prior facts; every value is stored, whatever the source's
    ``storage`` label (metadata only since the 8 Oct ruling).

    CHARGE BEFORE ANY PAID CALL (founder ruling, 8 Oct). The CALLER claims the quote
    (``start_run`` with :func:`claim_record`: one run per quote, ``DuplicateRunError``
    when it already ran) and captures the payment, and only then calls ``execute``.
    ``execute`` refuses (``RunRefusedError``, nothing called) unless the store holds that
    claim, still ``running``, with a payment in :data:`~gecko.anyquery.types.PAID`.

    ``execute`` then OWNS the rest of the run's writes, in this order: the call rows (the
    spend record), the facts, then ``finish_run``, keeping the claim's payment status.
    Any error after the first call leaves the run in the store for the caller to settle.
    """

    def __call__(
        self, plan: QueryPlan, quote: Quote, deps: RunDeps, *, run_id: str
    ) -> RunResult: ...


def claim_record(
    plan: QueryPlan,
    quote: Quote,
    *,
    run_id: str,
    mode: CallMode,
    now: str,
    payment_status: PaymentStatus = "pending",
) -> RunRecord:
    """The record that claims ``quote`` for one run, before it is paid or run."""
    return RunRecord(
        run_id=run_id,
        customer=plan.customer,
        plan_id=plan.plan_id,
        quote_id=quote.quote_id,
        mode=mode,
        status="running",
        payment_status=payment_status,
        started_at=now,
        finished_at=None,
        upstream_quoted_cents=quote.upstream_max_cents,
        upstream_charged_cents=0,
        calls=0,
        skipped_calls=0,
        counts={s: 0 for s in FIELD_STATES},
        stop_code=None,
        notices=(),
    )


def new_run_id(now: dt.datetime | None = None, token: str | None = None) -> str:
    """``YYYYMMDD-HHMMSS-<6hex>`` in UTC."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    suffix = token if token is not None else secrets.token_hex(3)
    run_id = f"{moment:%Y%m%d-%H%M%S}-{suffix}"
    if not RUN_ID_RE.match(run_id):
        raise ValueError("run id token must be 6 lowercase hex characters")
    return run_id
