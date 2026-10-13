"""The commenter-list job: plan (free), then run (paid, only when confirmed).

    plan_list  -> reads the sheet, the balance and the price; spends nothing
    run_list   -> refuses without ``confirm=True``; pays for page 1, follows the cursor
                  for free, matches in code, writes the list and the receipt

Recorded and live run every line here. ``deps_for`` is the only place they differ: a
made-up Orthogonal on the transport seam with no key and an in-memory ledger, or the
wire with the keyring key and the persistent ledger.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..access import AuthSession, public_session
from ..modes import CallMode
from .api import OrthogonalApi, Transport, live_transport
from .errors import (
    NO_HANDLE_COLUMN_NOTICE,
    ListJobError,
    NotConfirmedError,
    OutOfCreditsError,
    zero_comments_notice,
)
from .extract import balance_cents, details_price_cents
from .fixture import RecordedOrthogonal
from .match import counts, match_all
from .paginate import MAX_PAGES, fetch_commenters
from .post import canonical_post_url, post_id_from_url
from .record import Receipt, prune_outputs, write_outputs
from .session import orthogonal_session
from .sheet import CustomerSheet, default_folder, read_customers
from .spec import (
    BALANCE_OP,
    COMMENTS_API,
    COMMENTS_PATH,
    DETAILS_OP,
    PINNED_PRICE_CENTS,
)
from .spend import (
    PER_RUN_CAP_CENTS,
    REHEARSAL_CAP_CENTS,
    FileLedger,
    Ledger,
    MemoryLedger,
    SpendGuard,
)

FIRST_ROWS = 10


@dataclass
class JobDeps:
    api: OrthogonalApi
    ledger: Ledger
    folder: Path
    mode: CallMode
    clock: Callable[[], float] = time.time
    per_run_cap: int = PER_RUN_CAP_CENTS
    total_cap: int = REHEARSAL_CAP_CENTS
    max_pages: int = MAX_PAGES


def deps_for(
    mode: CallMode,
    *,
    folder: Path | None = None,
    ledger: Ledger | None = None,
    transport: Transport | None = None,
    session: AuthSession | None = None,
) -> JobDeps:
    folder = folder if folder is not None else default_folder()
    if mode == "live":
        api = OrthogonalApi(
            transport=transport or live_transport,
            session=session if session is not None else orthogonal_session(),
        )
        return JobDeps(api=api, ledger=ledger or FileLedger(), folder=folder, mode=mode)
    api = OrthogonalApi(
        transport=transport or RecordedOrthogonal(),
        session=session if session is not None else public_session(),
    )
    return JobDeps(
        api=api, ledger=ledger or MemoryLedger(), folder=folder, mode="recorded"
    )


@dataclass
class Plan:
    post_id: str
    quote_cents: int
    per_run_cap_cents: int
    rehearsal_left_cents: int
    balance: str | None
    sheet_rows: int
    has_handle_column: bool
    notices: list[str] = field(default_factory=list)
    message: str = ""


def _target() -> dict[str, Any]:
    return {"api": COMMENTS_API, "path": COMMENTS_PATH}


def _plan(deps: JobDeps, post_url: str, customers: str) -> tuple[Plan, CustomerSheet]:
    post_id = post_id_from_url(post_url)
    # The sheet first: a wrong file is refused before any call, even a free one.
    sheet = read_customers(customers, deps.folder)
    balance_raw = deps.api.call(BALANCE_OP)
    balance_text = balance_raw.get("balance") if isinstance(balance_raw, dict) else None
    quoted = details_price_cents(deps.api.call(DETAILS_OP, _target()))
    quote = max(PINNED_PRICE_CENTS, quoted or 0)
    guard = SpendGuard(deps.ledger, deps.per_run_cap, deps.total_cap)
    guard.check_fits(quote)
    have = balance_cents(balance_raw)
    if have is not None and have < quote:
        raise OutOfCreditsError(str(balance_text), quote)
    notices = [] if sheet.has_handle_column else [NO_HANDLE_COLUMN_NOTICE]
    plan = Plan(
        post_id=post_id,
        quote_cents=quote,
        per_run_cap_cents=deps.per_run_cap,
        rehearsal_left_cents=deps.total_cap - deps.ledger.total_cents(),
        balance=balance_text if isinstance(balance_text, str) else None,
        sheet_rows=sheet.rows,
        has_handle_column=sheet.has_handle_column,
        notices=notices,
        message=(
            f"1 paid call, ${quote / 100:.2f}. Later pages should be $0.00, and "
            "Gecko stops if one is not. "
            f"Cap ${deps.per_run_cap / 100:.2f}. Continue?"
        ),
    )
    return plan, sheet


def plan_list(deps: JobDeps, post_url: str, customers: str) -> Plan:
    """Everything she needs to say yes or no. Spends nothing."""
    return _plan(deps, post_url, customers)[0]


@dataclass
class ListResult:
    counts: dict[str, int]
    first_rows: list[dict[str, str]]
    list_path: str
    receipt_path: str
    receipt: dict[str, Any]
    notices: list[str]
    error: str | None = None
    code: str | None = None
    footer: str = "Gecko sends nothing. You decide what to do with the list."

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_list(
    deps: JobDeps, post_url: str, customers: str, *, confirm: bool
) -> ListResult:
    if confirm is not True:
        raise NotConfirmedError(
            "Nothing was spent. Show her the plan first, and run again with "
            "confirm=true only after she says yes."
        )
    now = deps.clock()
    prune_outputs(deps.folder, now)
    plan, sheet = _plan(deps, post_url, customers)
    guard = SpendGuard(deps.ledger, deps.per_run_cap, deps.total_cap)
    fetched = fetch_commenters(
        deps.api,
        canonical_post_url(post_url),
        guard,
        plan.quote_cents,
        max_pages=deps.max_pages,
    )
    rows = match_all(fetched.commenters, sheet)
    notices = list(plan.notices)
    if not fetched.commenters and fetched.abort is None:
        notices.append(zero_comments_notice(guard.spent))
    try:
        after = deps.api.call(BALANCE_OP)
        balance_after = after.get("balance") if isinstance(after, dict) else None
    except ListJobError:
        balance_after = None
    receipt = Receipt(
        mode=deps.mode,
        post_id=plan.post_id,
        endpoint=f"{COMMENTS_API} {COMMENTS_PATH}",
        calls=fetched.calls,
        # What she was shown and said yes to; per-page quotes stay on each call line.
        quoted_cents=plan.quote_cents,
        charged_cents=guard.spent,
        balance_before=plan.balance,
        balance_after=balance_after if isinstance(balance_after, str) else None,
        pages=len(fetched.calls),
        stop_reason=fetched.stop_reason,
        counts=counts(rows),
        rehearsal_spent_cents=deps.ledger.total_cents(),
        notices=notices,
    )
    list_path, receipt_path = write_outputs(deps.folder, rows, receipt, now)
    return ListResult(
        counts=receipt.counts,
        first_rows=[{"handle": r.handle, "match": r.match} for r in rows[:FIRST_ROWS]],
        list_path=str(list_path),
        receipt_path=str(receipt_path),
        receipt=receipt.to_dict(),
        notices=notices,
        error=fetched.abort.message if fetched.abort else None,
        code=fetched.abort.code if fetched.abort else None,
    )
