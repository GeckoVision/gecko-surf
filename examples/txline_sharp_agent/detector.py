"""Sharp Movement Detector — pure logic over TxLINE ``OddsPayload`` snapshots.

No network, no Gecko, no LLM here. Given successive odds snapshots (exactly what
TxLINE's ``/api/odds/*`` endpoints return — arrays of ``OddsPayload``), it tracks the
implied probability (``Pct``) for each unique price line and flags a **sharp move**
when that probability shifts by more than a threshold between observations. That signal
— fixture, book, market, outcome, how far and which way — is what a trading agent or an
on-chain prediction market acts on.

Why ``Pct`` and not ``Prices``: TxLINE already normalizes each outcome to an implied
probability (a string like ``"52.632"``, or ``"NA"`` for quarter-handicap lines), so a
delta in percentage points is directly comparable across books and markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# A price line's identity: which fixture, which book, which market, which outcome.
PriceKey = tuple[int, int, str, str]  # (FixtureId, BookmakerId, market, PriceName)


@dataclass(frozen=True)
class SharpMove:
    """One flagged shift in an outcome's implied probability."""

    fixture_id: int
    bookmaker: str
    market: str
    outcome: str
    old_pct: float
    new_pct: float
    delta: float  # new_pct - old_pct, in percentage points (signed)
    ts: int

    @property
    def direction(self) -> str:
        return "up" if self.delta > 0 else "down"

    def summary(self) -> str:
        return (
            f"[{self.market}] fixture {self.fixture_id} · {self.bookmaker} · "
            f"{self.outcome}: {self.old_pct:.3f}% → {self.new_pct:.3f}% "
            f"({self.delta:+.3f} pp, {self.direction})"
        )


def _market_id(payload: dict[str, Any]) -> str:
    """A stable market label so the same line matches across snapshots."""
    return f"{payload.get('SuperOddsType', '')}|{payload.get('MarketParameters', '')}"


class SharpDetector:
    """Stateful across observations: remembers the last ``Pct`` per price line.

    ``observe`` a snapshot (a list of ``OddsPayload`` dicts) and get back the moves that
    crossed ``threshold_pct`` since the previous observation of that same line. The first
    observation of any line only sets the baseline — it never fires.
    """

    def __init__(self, *, threshold_pct: float = 3.0) -> None:
        if threshold_pct <= 0:
            raise ValueError("threshold_pct must be positive")
        self.threshold = threshold_pct
        self._last: dict[PriceKey, float] = {}

    def observe(self, payloads: list[dict[str, Any]]) -> list[SharpMove]:
        moves: list[SharpMove] = []
        for p in payloads:
            fixture_id = p.get("FixtureId")
            book_id = p.get("BookmakerId")
            if fixture_id is None or book_id is None:
                continue
            bookmaker = p.get("Bookmaker", "")
            market = _market_id(p)
            ts = p.get("Ts", 0)
            names = p.get("PriceNames") or []
            pcts = p.get("Pct") or []
            for name, pct_raw in zip(names, pcts):
                pct = _parse_pct(pct_raw)
                if pct is None:  # "NA" / malformed line — skip, never crash
                    continue
                key: PriceKey = (fixture_id, book_id, market, name)
                prev = self._last.get(key)
                self._last[key] = pct
                if prev is None:
                    continue  # baseline only
                delta = round(pct - prev, 3)
                if abs(delta) >= self.threshold:
                    moves.append(
                        SharpMove(
                            fixture_id=fixture_id,
                            bookmaker=bookmaker,
                            market=market,
                            outcome=name,
                            old_pct=prev,
                            new_pct=pct,
                            delta=delta,
                            ts=ts,
                        )
                    )
        return moves


def _parse_pct(raw: Any) -> float | None:
    """TxLINE ``Pct`` is a 3-dp string or ``"NA"``. Return a float or None."""
    if raw is None or raw == "NA":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
