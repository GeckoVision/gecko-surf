"""Token price and liquidity from Jupiter; the real share price from Financial Datasets.

JUPITER. Tokens v2 search answers a LIST of tokens for a query. A search can return
look-alikes (same symbol, another mint), so only the entry whose ``id`` IS the mint is
read — never the first one. ``usdPrice`` and ``liquidity`` are the field names seen in
the reply on 2026-10-08.

SHARE PRICE. One paid Orthogonal call (``financial-datasets /prices/snapshot``,
query ``ticker`` as a string). ``/v1/details`` lists no response fields for it, so the
reader looks for a ``snapshot`` object and a ``price`` in it, checks the ticker when the
reply names one, and returns ``None`` when nothing fits — "no data", never a zero.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..netguard import UnsafeUrlError
from ..orthogonal.spec import FD_PRICE_SNAPSHOT
from ..orthogonal.steps import PublicStep
from ..orthogonal.supplier.calls import CallSpec
from ..orthogonal.supplier.readers import data_of, field_names, find_first
from .chain import decimal_of
from .wire import PublicFetch
from .xstocks import XStock

KEY_SHARE_PRICE = "fd_price_snapshot"
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.]{5,15})?(Z|[+-]\d{2}:?\d{2})?$")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class TokenMarket:
    usd_price: Decimal | None
    liquidity_usd: Decimal | None
    step: PublicStep
    #: Our words for why a value is missing; empty when both were read.
    reason: str = ""


def read_market(url: str, mint: str, fetch: PublicFetch) -> TokenMarket:
    path = "/tokens/v2/search"
    try:
        status, text = fetch(url)
    except UnsafeUrlError:
        step = PublicStep(path, ("query",), None, (), 0, "refused")
        return TokenMarket(
            None, None, step, "the network guard refused the Jupiter URL"
        )
    except OSError:
        step = PublicStep(path, ("query",), None, (), 0, "unreachable")
        return TokenMarket(None, None, step, "Jupiter could not be reached")
    try:
        body: Any = json.loads(text, parse_float=Decimal) if text else None
    except ValueError:
        body = None
    tokens = body if isinstance(body, list) else _dict(body).get("tokens")
    items = (
        [t for t in tokens if isinstance(t, dict)] if isinstance(tokens, list) else []
    )
    step = PublicStep(
        path,
        ("query",),
        status,
        tuple(field_names(items[0] if items else body)),
        len(items),
        None if 200 <= status < 300 else "upstream_error",
    )
    if step.error:
        return TokenMarket(None, None, step, f"Jupiter answered HTTP {status}")
    mine = next((t for t in items if t.get("id") == mint), None)
    if mine is None:
        return TokenMarket(
            None, None, step, "Jupiter's answer had no entry for this mint"
        )
    price = decimal_of(mine.get("usdPrice"))
    liquidity = decimal_of(mine.get("liquidity"))
    reason = (
        ""
        if price is not None and liquidity is not None
        else ("Jupiter's entry lacked a usable usdPrice or liquidity")
    )
    return TokenMarket(price, liquidity, step, reason)


def share_price_call(xstock: XStock) -> CallSpec:
    return CallSpec(
        KEY_SHARE_PRICE,
        FD_PRICE_SNAPSHOT,
        FD_PRICE_SNAPSHOT.path,
        query={"ticker": xstock.ticker},
        subject=xstock.mint,
    )


@dataclass(frozen=True)
class SharePrice:
    price: Decimal | None
    #: The snapshot's own time when it carried one in an ISO shape; else ``None``.
    as_of: str | None = None
    reason: str = ""


def read_share_price(payload: Any, ticker: str) -> SharePrice:
    data = data_of(payload)
    snapshot = find_first(data, ("snapshot",))
    node = snapshot if isinstance(snapshot, dict) else _dict(data)
    named = node.get("ticker")
    if isinstance(named, str) and named.strip().upper() != ticker.upper():
        return SharePrice(None, reason="the reply named a different ticker")
    price = decimal_of(node.get("price"))
    if price is None or price == 0:
        return SharePrice(None, reason="the reply carried no usable price")
    when = node.get("time") or node.get("date")
    as_of = when if isinstance(when, str) and _ISO_TIME.match(when) else None
    return SharePrice(price, as_of)
