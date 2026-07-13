"""The synthetic feed is deterministic and moves exactly once, at move_at."""

from __future__ import annotations

from examples.txline_sharp_agent.detector import SharpDetector
from examples.txline_sharp_agent.synthetic import scripted_feed


def test_feed_is_schema_valid_and_deterministic():
    a = scripted_feed()
    b = scripted_feed()
    assert a == b  # no RNG
    book = a[0][0]
    # required OddsPayload fields present
    for field in (
        "FixtureId",
        "Ts",
        "Bookmaker",
        "BookmakerId",
        "SuperOddsType",
        "InRunning",
    ):
        assert field in book
    assert len(book["PriceNames"]) == len(book["Pct"]) == 3


def test_sharp_move_fires_only_at_move_at():
    det = SharpDetector(threshold_pct=3.0)
    fired_ticks = []
    for tick, snapshot in enumerate(scripted_feed(move_at=3, ticks=6)):
        if det.observe(snapshot):
            fired_ticks.append(tick)
    assert fired_ticks == [3]  # drift stays sub-threshold; only the scripted jump fires
