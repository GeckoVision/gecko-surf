"""Page through one logical Orthogonal search: pay for page 1, follow on for free.

Continuation pages are free only when they carry page 1's ``requestId`` as
``parentRequestId`` and keep every non-pagination param unchanged. Anything else bills
every page, and a cursor that repeats would loop forever, so the loop has four stops:
no cursor, a repeated cursor, an empty follow-up page, and a hard page cap. A follow-up
that charges anything at all ends the run as a failure.

Once money has moved, a failure does not raise: it is returned as ``abort`` beside the
calls already made, so the receipt still says what was charged.

A call that FAILS after the upstream may have charged for it (``errors.charge_possible``:
a reply that is not a success, a wire that broke after sending, a paid x402 retry that
did not come back) is counted at its quote: the hold is kept, not released (founder-
approved, 9 Oct). Over-counting a few cents is the safe direction for a spend cap; a
release would let the next call spend money the upstream already took. A refusal made
before the request left still releases its hold.

``fetch_pages`` is the one loop; the commenter list and the supplier watch hand it a
body builder and a page reader. A single-shot call is the same loop with one page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .api import OrthogonalApi
from .errors import (
    ContinuationChargedError,
    ListJobError,
    UpstreamError,
    charge_possible,
)
from .extract import Commenter, billing_facts, page_facts, payment_receipt
from .spec import COMMENTS_API, COMMENTS_PATH, RUN_OP
from .spend import SpendGuard

MAX_PAGES = 10


@dataclass(frozen=True)
class CallLine:
    page: int
    request_id: str | None
    parent_request_id: str | None
    quoted_cents: int | None
    charged_cents: int
    #: True when the reply carried no ``priceCents`` and the quote was counted instead.
    charge_assumed: bool = False
    #: ``api path-template`` of the call; empty on the commenter job's lines.
    endpoint: str = ""
    #: True for a call that failed after it may have been charged (counted at quote).
    failed: bool = False
    #: The x402 receipt, when the call was paid on chain: the payment's signature (from
    #: the server's settlement answer, when it gave one) and what was paid.
    payment_signature: str | None = None
    paid_usdc_micro: int | None = None


@dataclass(frozen=True)
class PageRead:
    """What a reader took from one page. ``items is None`` means an unknown shape."""

    items: tuple[Any, ...] | None
    cursor: str | None = None
    #: Field NAMES under ``data`` — never values — to diagnose an unknown shape.
    data_fields: tuple[str, ...] = ()


#: (cursor, parentRequestId) -> the ``/v1/run`` body for that page.
BodyFor = Callable[[str | None, str | None], dict[str, Any]]
ReadPage = Callable[[Any], PageRead]


@dataclass
class FetchResult:
    items: list[Any] = field(default_factory=list)
    calls: list[CallLine] = field(default_factory=list)
    stop_reason: str = ""
    abort: ListJobError | None = None
    #: Parsed replies, held in memory for the reader of this run only. Never stored.
    payloads: list[Any] = field(default_factory=list)

    @property
    def commenters(self) -> list[Commenter]:
        return self.items


def fetch_pages(
    api: OrthogonalApi,
    body_for: BodyFor,
    read: ReadPage,
    guard: SpendGuard,
    quote_cents: int,
    *,
    max_pages: int = MAX_PAGES,
    what: str = "data list",
    endpoint: str = "",
) -> FetchResult:
    result = FetchResult()
    parent: str | None = None
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for page in range(1, max_pages + 1):
        # Built before the hold: a body that cannot be built never sent anything.
        body = body_for(cursor, parent)
        try:
            # Every call is bounded at full price: a "free" page is a claim, not a fact.
            guard.check_before(quote_cents)
            try:
                payload = api.call(RUN_OP, body)
            except BaseException as exc:
                if not charge_possible(exc):
                    # Refused before the request left: nothing to count, on any ledger.
                    guard.release()
                    raise
                # It may have been charged: keep the hold, counted at the quote.
                guard.record(quote_cents)
                if isinstance(exc, ListJobError):
                    exc.counted_cents = quote_cents
                if page > 1:
                    result.payloads.append(None)
                    result.calls.append(
                        CallLine(
                            page=page,
                            request_id=None,
                            parent_request_id=parent,
                            quoted_cents=quote_cents,
                            charged_cents=quote_cents,
                            charge_assumed=True,
                            endpoint=endpoint,
                            failed=True,
                        )
                    )
                raise
        except ListJobError as exc:
            if page == 1:
                raise
            result.abort, result.stop_reason = exc, exc.code
            return result
        bill = billing_facts(payload)
        receipt = payment_receipt(payload)
        read_page = read(payload)
        charged = bill.charged_cents
        assumed = charged is None
        if charged is None:
            charged = quote_cents
        guard.record(charged)
        result.payloads.append(payload)
        result.calls.append(
            CallLine(
                page=page,
                request_id=bill.request_id,
                parent_request_id=parent,
                quoted_cents=bill.quoted_cents,
                charged_cents=charged,
                charge_assumed=assumed,
                endpoint=endpoint,
                payment_signature=receipt.signature if receipt else None,
                paid_usdc_micro=receipt.amount_usdc_micro if receipt else None,
            )
        )
        if read_page.items is None:
            result.abort = UpstreamError(
                f"Orthogonal's reply has no {what} (fields seen: "
                f"{', '.join(read_page.data_fields) or 'none'}). Nothing was matched."
            )
            result.stop_reason = result.abort.code
            return result
        if page > 1 and charged > 0:
            result.abort = ContinuationChargedError(
                f"Stopped: page {page} charged ${charged / 100:.2f}, but follow-up "
                "pages should be free. No more pages were fetched."
            )
            result.stop_reason = result.abort.code
            return result
        result.items.extend(read_page.items)
        if page == 1:
            parent = bill.request_id
            if parent is None:
                result.stop_reason = "no_request_id"
                return result
        if not read_page.items and page > 1:
            result.stop_reason = "empty_page"
            return result
        if read_page.cursor is None:
            result.stop_reason = "last_page"
            return result
        if read_page.cursor in seen_cursors:
            result.stop_reason = "repeated_cursor"
            return result
        seen_cursors.add(read_page.cursor)
        cursor = read_page.cursor
    result.stop_reason = "page_cap"
    return result


def _comments_body(post_url: str) -> BodyFor:
    def body_for(cursor: str | None, parent: str | None) -> dict[str, Any]:
        query: dict[str, Any] = {"url": post_url}
        if cursor is not None:
            query["cursor"] = cursor
        body: dict[str, Any] = {
            "api": COMMENTS_API,
            "path": COMMENTS_PATH,
            "query": query,
        }
        if parent is not None:
            body["parentRequestId"] = parent
        return body

    return body_for


def _read_comments(payload: Any) -> PageRead:
    facts = page_facts(payload)
    return PageRead(
        items=facts.commenters, cursor=facts.cursor, data_fields=facts.data_fields
    )


def fetch_commenters(
    api: OrthogonalApi,
    post_url: str,
    guard: SpendGuard,
    quote_cents: int,
    max_pages: int = MAX_PAGES,
) -> FetchResult:
    return fetch_pages(
        api,
        _comments_body(post_url),
        _read_comments,
        guard,
        quote_cents,
        max_pages=max_pages,
        what="comment list",
    )
