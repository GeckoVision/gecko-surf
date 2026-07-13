"""Synthetic TxLINE odds feed — a deterministic, schema-valid local simulation.

Recorded mode ($0) synthesizes *one* schema-shaped response, so two recorded calls look
identical — great for proving a call is first-call-correct, useless for showing movement.
To simulate a *live market* offline, this module scripts a timeline of ``OddsPayload``
snapshots for one fixture: the implied probabilities drift gently (below the detector's
threshold), then take one **sharp move** at a chosen tick — exactly the scenario a trading
agent must catch. No RNG, so the demo and its tests are reproducible.

Each emitted snapshot matches TxLINE's ``OddsPayload`` schema (the same shape Gecko
comprehends from ``txline_openapi.yaml``), so the detector and agent run against the real
contract — only the transport is synthetic.
"""

from __future__ import annotations

from typing import Any

# A 1x2 (match-result) market: three outcomes whose implied probabilities sum ~100%.
_OUTCOMES = ("Home", "Draw", "Away")


def _payload(
    *,
    fixture_id: int,
    bookmaker: str,
    book_id: int,
    pcts: dict[str, float],
    ts: int,
) -> dict[str, Any]:
    """One bookmaker's 1x2 offer at time ``ts``, in TxLINE ``OddsPayload`` shape."""
    names = list(_OUTCOMES)
    return {
        "FixtureId": fixture_id,
        "MessageId": f"syn-{fixture_id}-{book_id}-{ts}",
        "Ts": ts,
        "Bookmaker": bookmaker,
        "BookmakerId": book_id,
        "SuperOddsType": "1x2",
        "GameState": "InPlay",
        "InRunning": True,
        "MarketParameters": "",
        "MarketPeriod": "FT",
        "PriceNames": names,
        # decimal odds ≈ 100/prob, ×1000 as TxLINE integer prices (illustrative).
        "Prices": [round(100_000 / pcts[n]) if pcts[n] else 0 for n in names],
        "Pct": [f"{pcts[n]:.3f}" for n in names],
    }


def scripted_feed(
    *,
    fixture_id: int = 42,
    bookmaker: str = "Pinnacle",
    book_id: int = 3,
    base: dict[str, float] | None = None,
    drift: float = 0.2,
    move_at: int = 3,
    move: dict[str, float] | None = None,
    ticks: int = 6,
    start_ts: int = 1_700_000_000_000,
    step_ms: int = 60_000,
) -> list[list[dict[str, Any]]]:
    """A timeline of odds snapshots: gentle drift, then one sharp move at ``move_at``.

    Returns ``ticks`` snapshots (each a one-element list of ``OddsPayload``, matching the
    ``/api/odds/updates/{fixtureId}`` array shape). ``base`` is the opening implied-prob
    split (defaults ~45/27/28). Each tick nudges Home by ``drift`` pp (sub-threshold);
    at ``move_at`` the ``move`` deltas are applied on top — the sharp move to catch.
    """
    probs = dict(base or {"Home": 45.0, "Draw": 27.0, "Away": 28.0})
    jump = dict(move or {"Home": 9.0, "Away": -9.0})
    feed: list[list[dict[str, Any]]] = []
    for tick in range(ticks):
        # sub-threshold drift each tick, keeping the book roughly balanced.
        probs["Home"] = round(probs["Home"] + drift, 3)
        probs["Away"] = round(probs["Away"] - drift, 3)
        if tick == move_at:
            for outcome, delta in jump.items():
                probs[outcome] = round(probs[outcome] + delta, 3)
        ts = start_ts + tick * step_ms
        feed.append(
            [
                _payload(
                    fixture_id=fixture_id,
                    bookmaker=bookmaker,
                    book_id=book_id,
                    pcts=probs,
                    ts=ts,
                )
            ]
        )
    return feed
