"""Read the few fields the job needs out of an Orthogonal reply, and drop the rest.

Shape measured live on 2026-10-08 (``/v1/run`` on scrapecreators post comments):
top level ``success, priceCents, quotedPriceCents, requestId, billing, data``;
``billing`` carries ``requestId, parentRequestId, rootRequestId, continuation,
chargedPriceCents``; ``data`` carries ``comments[]`` (``id, text, created_at, user``)
and ``cursor``. The ``user`` sub-fields were not inspected, so the handle is read
defensively and a missing one is kept as ``None`` (it lands in "not sure").

Comment TEXT is never read: it is attacker-written, and the only thing that leaves
this module about a comment is who wrote it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Commenter:
    handle: str | None
    full_name: str | None = None


@dataclass(frozen=True)
class Billing:
    """The billing facts every ``/v1/run`` reply carries, whatever the upstream."""

    request_id: str | None
    parent_request_id: str | None
    root_request_id: str | None
    charged_cents: int | None
    quoted_cents: int | None
    continuation: bool | None


#: Where an x402-paid call's receipt rides in the envelope Gecko itself builds around the
#: reply (``gecko.orthogonal.api``). Stripped from every key-path reply, so an upstream
#: cannot plant one.
X402_RECEIPT_KEY = "_gecko_x402"


@dataclass(frozen=True)
class PaymentReceipt:
    """What one x402 call paid: the amount Gecko signed, and the transaction signature
    the server's settlement answer reported (``None`` when it reported none)."""

    signature: str | None
    amount_usdc_micro: int
    pay_to: str


def payment_receipt(payload: Any) -> PaymentReceipt | None:
    """The x402 receipt Gecko wrote into ``payload``, or ``None`` (key path)."""
    raw = _dict(_dict(payload).get(X402_RECEIPT_KEY))
    amount = raw.get("amount_usdc_micro")
    if not isinstance(amount, int) or isinstance(amount, bool):
        return None
    return PaymentReceipt(
        signature=_str(raw.get("signature")),
        amount_usdc_micro=amount,
        pay_to=_str(raw.get("pay_to")) or "",
    )


def billing_facts(payload: Any) -> Billing:
    top = _dict(payload)
    billing = _dict(top.get("billing"))
    charged = cents_up(top.get("priceCents"))
    if charged is None:
        charged = cents_up(billing.get("chargedPriceCents"))
    quoted = cents_up(top.get("quotedPriceCents"))
    if quoted is None:
        quoted = cents_up(billing.get("quotedPriceCents"))
    continuation = billing.get("continuation")
    return Billing(
        request_id=_str(top.get("requestId")) or _str(billing.get("requestId")),
        parent_request_id=_str(billing.get("parentRequestId")),
        root_request_id=_str(billing.get("rootRequestId")),
        charged_cents=charged,
        quoted_cents=quoted,
        continuation=continuation if isinstance(continuation, bool) else None,
    )


@dataclass(frozen=True)
class PageFacts:
    request_id: str | None
    parent_request_id: str | None
    root_request_id: str | None
    charged_cents: int | None
    quoted_cents: int | None
    continuation: bool | None
    #: ``None`` when the reply carried no comment list at all (an unknown shape).
    commenters: tuple[Commenter, ...] | None
    cursor: str | None
    #: Field NAMES under ``data`` — never values — to diagnose an unknown shape.
    data_fields: tuple[str, ...] = ()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def cents_up(value: Any) -> int | None:
    """Cents as Orthogonal reports them, rounded UP to a whole cent.

    A $0.025 call reports ``2.5``; the cap counts it as 3 so it can never under-count.
    """
    whole = _int(value)
    if whole is not None:
        return whole
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return math.ceil(round(value, 6))
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _commenter(item: Any) -> Commenter:
    user = _dict(_dict(item).get("user"))
    handle = _str(user.get("username")) or _str(user.get("handle"))
    return Commenter(handle=handle, full_name=_str(user.get("full_name")))


def page_facts(payload: Any) -> PageFacts:
    """The facts of one ``/v1/run`` page. ``commenters`` is ``None`` when there is no
    comment list at all, so an unknown shape is never reported as "0 comments"."""
    top = _dict(payload)
    data = _dict(top.get("data"))
    comments = data.get("comments")
    bill = billing_facts(payload)
    return PageFacts(
        request_id=bill.request_id,
        parent_request_id=bill.parent_request_id,
        root_request_id=bill.root_request_id,
        charged_cents=bill.charged_cents,
        quoted_cents=bill.quoted_cents,
        continuation=bill.continuation,
        commenters=(
            tuple(_commenter(item) for item in comments)
            if isinstance(comments, list)
            else None
        ),
        cursor=_str(data.get("cursor")),
        data_fields=tuple(sorted(str(k) for k in data)),
    )


_DOLLARS = re.compile(r"^\s*\$?\s*(\d+)(?:\.(\d{1,2}))?\s*$")


def dollars_to_cents(text: Any) -> int | None:
    """``"$1.00"`` -> ``100``; anything else -> ``None``."""
    if not isinstance(text, str):
        return None
    match = _DOLLARS.match(text)
    if match is None:
        return None
    whole, frac = match.group(1), (match.group(2) or "0").ljust(2, "0")
    return int(whole) * 100 + int(frac)


def balance_cents(payload: Any) -> int | None:
    return dollars_to_cents(_dict(payload).get("balance"))


def details_price_cents(payload: Any) -> int | None:
    """The quoted price from ``/v1/details``, wherever it sits; ``None`` if unreadable."""
    seen: list[dict[str, Any]] = [_dict(payload)]
    while seen:
        node = seen.pop()
        for key in ("priceCents", "quotedPriceCents"):
            cents = _int(node.get(key))
            if cents is not None:
                return cents
        price = node.get("price")
        cents = dollars_to_cents(price)
        if (
            cents is None
            and isinstance(price, (int, float))
            and not isinstance(price, bool)
        ):
            # ``/v1/details`` states the price in DOLLARS as a number (0.04).
            cents = cents_up(float(price) * 100)
        if cents is not None:
            return cents
        seen.extend(v for v in node.values() if isinstance(v, dict))
    return None
