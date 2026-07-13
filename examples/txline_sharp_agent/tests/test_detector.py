"""Sharp Movement Detector — pure-logic tests, no network, no LLM."""

from __future__ import annotations

from examples.txline_sharp_agent.detector import SharpDetector, SharpMove


def _payload(pct_home: str, pct_away: str, *, ts: int = 1000) -> dict:
    """One bookmaker's 1x2 market for fixture 42, book 7, at time `ts`."""
    return {
        "FixtureId": 42,
        "MessageId": f"m-{ts}",
        "Ts": ts,
        "Bookmaker": "Acme",
        "BookmakerId": 7,
        "SuperOddsType": "1x2",
        "MarketParameters": "",
        "InRunning": True,
        "PriceNames": ["Home", "Away"],
        "Pct": [pct_home, pct_away],
    }


def test_first_observation_sets_baseline_no_signal():
    det = SharpDetector(threshold_pct=3.0)
    assert det.observe([_payload("50.000", "50.000")]) == []


def test_move_above_threshold_flags_signal_with_direction():
    det = SharpDetector(threshold_pct=3.0)
    det.observe([_payload("50.000", "50.000", ts=1000)])
    both = det.observe([_payload("55.000", "45.000", ts=1060)])
    # Home +5.0 and Away -5.0 both exceed 3.0 → both flagged
    assert len(both) == 2
    home = next(m for m in both if m.outcome == "Home")
    assert isinstance(home, SharpMove)
    assert home.fixture_id == 42 and home.bookmaker == "Acme"
    assert home.old_pct == 50.0 and home.new_pct == 55.0
    assert home.delta == 5.0 and home.direction == "up" and home.ts == 1060
    away = next(m for m in both if m.outcome == "Away")
    assert away.delta == -5.0 and away.direction == "down"


def test_move_below_threshold_is_silent():
    det = SharpDetector(threshold_pct=3.0)
    det.observe([_payload("50.000", "50.000")])
    assert det.observe([_payload("52.000", "48.000")]) == []  # ±2.0 < 3.0


def test_na_and_unparseable_pct_ignored():
    det = SharpDetector(threshold_pct=1.0)
    det.observe([_payload("50.000", "NA")])
    # Away was NA (skipped), Home moves +10 → one signal, no crash on the NA line
    moves = det.observe([_payload("60.000", "bogus")])
    assert [m.outcome for m in moves] == ["Home"]


def test_distinct_price_lines_tracked_independently():
    """A move on book 7 must not be attributed to book 9 (same fixture/market)."""
    det = SharpDetector(threshold_pct=3.0)
    other = {**_payload("50.000", "50.000"), "BookmakerId": 9, "Bookmaker": "Zeta"}
    det.observe([_payload("50.000", "50.000"), other])
    moves = det.observe([_payload("58.000", "42.000")])  # only book 7 updates
    assert {m.bookmaker for m in moves} == {"Acme"}
