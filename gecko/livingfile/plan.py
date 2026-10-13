"""The free step: what the living file will read, and what the one paid read costs.

It asks Orthogonal for the balance and the share-price quote (both free), then refuses
— still free — when the quote would cross the per-run cap, the total cap or the
balance. Nothing else is read until the person says yes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..orthogonal.errors import OutOfCreditsError, SpendCapError
from ..orthogonal.extract import balance_cents, details_price_cents
from ..orthogonal.spec import BALANCE_OP, DETAILS_OP, FD_PRICE_SNAPSHOT
from .deps import XstockDeps
from .xstocks import host_of, xstock_for


@dataclass
class XstockPlan:
    mint: str
    symbol: str
    ticker: str
    #: (what, where, cents) per call, in call order. Only one costs anything.
    calls: list[tuple[str, str, int]]
    total_cents: int
    per_run_cap_cents: int
    total_left_cents: int
    balance: str | None
    message: str = ""


def _quote(deps: XstockDeps) -> int:
    body = {"api": FD_PRICE_SNAPSHOT.api, "path": FD_PRICE_SNAPSHOT.path}
    quoted = details_price_cents(deps.base.api.call(DETAILS_OP, body))
    return max(FD_PRICE_SNAPSHOT.pinned_cents, quoted or 0)


def plan_file(deps: XstockDeps, mint: str) -> XstockPlan:
    """Everything the person needs to say yes or no. Spends nothing."""
    xstock = xstock_for(mint)
    balance_raw = deps.base.api.call(BALANCE_OP)
    balance_text = balance_raw.get("balance") if isinstance(balance_raw, dict) else None
    price = _quote(deps)
    base = deps.base
    left = base.total_cap - base.ledger.total_cents()
    if price > base.per_run_cap:
        raise SpendCapError(
            f"The share price costs about ${price / 100:.2f}; one run is capped at "
            f"${base.per_run_cap / 100:.2f}. Nothing was spent."
        )
    if price > left:
        raise SpendCapError(
            f"The share price costs about ${price / 100:.2f}; ${left / 100:.2f} is left "
            f"of the ${base.total_cap / 100:.2f} total. Nothing was spent."
        )
    have = balance_cents(balance_raw)
    if have is not None and have < price:
        raise OutOfCreditsError(str(balance_text), price)
    rpc, jup = host_of(deps.rpc_url), host_of(deps.jupiter_url)
    calls = [
        ("supply, authorities, extensions", f"Solana RPC at {rpc}", 0),
        ("token price and liquidity", f"Jupiter at {jup}", 0),
        ("share price", f"{FD_PRICE_SNAPSHOT.label} ({xstock.ticker})", price),
        *[("legal terms", host_of(p.url), 0) for p in deps.legal_pages],
    ]
    return XstockPlan(
        mint=xstock.mint,
        symbol=xstock.symbol,
        ticker=xstock.ticker,
        calls=calls,
        total_cents=price,
        per_run_cap_cents=base.per_run_cap,
        total_left_cents=left,
        balance=balance_text if isinstance(balance_text, str) else None,
        message=(
            f"{xstock.symbol}: 1 paid call, the {xstock.ticker} share price, about "
            f"${price / 100:.2f}. Free: the mint on {rpc}, the token price on {jup}, "
            f"{len(deps.legal_pages)} legal pages. Reserves: not read (no feed "
            f"address). Cap ${base.per_run_cap / 100:.2f} per run, ${left / 100:.2f} "
            f"left of ${base.total_cap / 100:.2f}. Continue?"
        ),
    )
