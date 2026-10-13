"""The three checks, computed in code from this run's FRESH rows — red when they fail.

    price gap      (token - share) / share, in percent; red above the threshold
    reserves       supply x multiplier vs shares held; red when supply exceeds them
    legal terms    red when a legal page's hash moved since its last stored version

A check whose inputs are not all fresh this run is "not checked", with the reason: a
gap between a fresh token price and last week's share price is not a gap.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .facts import (
    CHECK_LEGAL,
    CHECK_PRICE_GAP,
    CHECK_RESERVES,
    RESERVES,
    SHARE_PRICE,
    SUPPLY_SCALED,
    TOKEN_PRICE,
    CheckResult,
    FactRow,
    Reading,
)
from .xstocks import RESERVES_NOT_CHECKED, XStock

SRC_COMPUTED = "computed in code"
_CENT = Decimal("0.01")


def plain(number: Decimal) -> str:
    """A Decimal as plain digits: ``1E+2`` -> ``100``, ``1.50`` -> ``1.5``."""
    text = format(number.normalize(), "f")
    return text


def _fresh(rows: dict[str, FactRow], name: str) -> Decimal | None:
    row = rows.get(name)
    if row is None or row.status != "fresh" or row.value is None:
        return None
    return Decimal(row.value)


def _not_checked(name: str, why: str, at: str) -> Reading:
    return Reading(
        name,
        None,
        SRC_COMPUTED,
        at,
        f"not checked: {why}",
        note=why,
        kind="check",
        check="not checked",
    )


def price_gap(rows: dict[str, FactRow], threshold: Decimal, at: str) -> Reading:
    token, share = _fresh(rows, TOKEN_PRICE), _fresh(rows, SHARE_PRICE)
    if token is None or share is None or share == 0:
        return _not_checked(
            CHECK_PRICE_GAP, "token or share price was not read this run", at
        )
    gap = ((token - share) / share * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    red = abs(gap) > threshold
    verdict: CheckResult = "red" if red else "ok"
    return Reading(
        CHECK_PRICE_GAP,
        f"{gap:+.2f}",
        SRC_COMPUTED,
        at,
        f"token {plain(token)} vs share {plain(share)}: gap {gap:+.2f}% "
        f"(threshold {plain(threshold)}%): {verdict}",
        note="per UI token against one share; outside market hours the share price "
        "is the last one the source had",
        kind="check",
        check=verdict,
    )


def supply_vs_reserves(rows: dict[str, FactRow], at: str) -> Reading:
    supply, held = _fresh(rows, SUPPLY_SCALED), _fresh(rows, RESERVES)
    if held is None:
        why = rows[RESERVES].note if RESERVES in rows else RESERVES_NOT_CHECKED
        return _not_checked(CHECK_RESERVES, why, at)
    if supply is None:
        return _not_checked(CHECK_RESERVES, "supply was not read this run", at)
    red = supply > held
    verdict: CheckResult = "red" if red else "ok"
    return Reading(
        CHECK_RESERVES,
        plain(held - supply),
        SRC_COMPUTED,
        at,
        f"supply x multiplier {plain(supply)} vs reserves {plain(held)}: {verdict}",
        note="Solana supply only; xStocks also live on other chains, so reserves "
        "cover more than this supply",
        kind="check",
        check=verdict,
    )


def legal_changed(legal: list[FactRow], at: str) -> Reading:
    unread = [r.field for r in legal if r.status != "fresh"]
    if unread:
        return _not_checked(CHECK_LEGAL, f"not read this run: {', '.join(unread)}", at)
    first = [r.field for r in legal if r.previous_value is None]
    if first:
        return _not_checked(
            CHECK_LEGAL, f"first version stored: {', '.join(first)}", at
        )
    moved = [r.field for r in legal if r.changed]
    verdict: CheckResult = "red" if moved else "ok"
    return Reading(
        CHECK_LEGAL,
        "changed" if moved else "unchanged",
        SRC_COMPUTED,
        at,
        f"legal terms changed since last version: {', '.join(moved)}"
        if moved
        else "legal terms unchanged since last version",
        kind="check",
        check=verdict,
    )


@dataclass(frozen=True)
class ReservesRead:
    """Shares held for one xStock, or ``None`` with the reason it was not read."""

    shares: Decimal | None
    source: str
    reason: str = ""


#: The reserves seam. The default reads nothing, on purpose: no address was found.
ReservesReader = Callable[[XStock], ReservesRead]


def reserves_not_checked(_xstock: XStock) -> ReservesRead:
    return ReservesRead(None, "Chainlink proof of reserve", RESERVES_NOT_CHECKED)
