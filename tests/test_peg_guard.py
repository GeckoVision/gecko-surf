"""The peg guard refuses on everything that is not a clean, fresh PEGGED.

Every payload here is the SHAPE Pegana actually returns — the stale one is the literal
body `/v1/assets/USDG/state` served while this was written, which is the case that made
the guard worth building: `state: UNKNOWN, stale: true, state_reason: "stale_source"`.
"""

from gecko.peg_guard import HOLDING, PEG_STATES, verdict_from

MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"

#: Measured 2026-08-27 from api.pegana.xyz — not invented.
LIVE_STALE = {
    "asset": "USDG",
    "state": "UNKNOWN",
    "since": "2026-08-26T16:19:02.758Z",
    "discount": "-0.000093",
    "intrinsic_usd": "1",
    "market_usd": "1.00008277",
    "updated_at": "2026-08-26T16:19:02.758Z",
    "stale": True,
    "state_reason": "stale_source",
    "confidence": "unknown",
}


def test_the_live_stale_reading_refuses() -> None:
    """The case that exists right now. A guard that waved this through would be the
    fail-open shape: silently ceasing to apply at the moment it might matter."""
    v = verdict_from(MINT, LIVE_STALE)
    assert v.outcome == "refuse"
    assert v.blocks is True
    assert "STALE" in v.reason


def test_a_fresh_pegged_reading_approves() -> None:
    v = verdict_from(MINT, {**LIVE_STALE, "state": "PEGGED", "stale": False})
    assert v.outcome == "ok"
    assert v.blocks is False


def test_a_stale_PEGGED_still_refuses() -> None:
    """Staleness is judged BEFORE the value: a PEGGED reading from a stale source is a
    statement about the past, and a guard is about the present."""
    v = verdict_from(MINT, {**LIVE_STALE, "state": "PEGGED", "stale": True})
    assert v.outcome == "refuse"
    assert "STALE" in v.reason


def test_every_non_holding_state_refuses() -> None:
    for state in sorted(PEG_STATES - HOLDING):
        v = verdict_from(MINT, {**LIVE_STALE, "state": state, "stale": False})
        assert v.outcome == "refuse", f"{state} must not be treated as convertible"
        assert v.blocks is True


def test_an_untracked_mint_is_unknown_and_does_not_block() -> None:
    """Pegana tracks 67 assets. Refusing every other mint would make this a blanket denial
    rather than a signal — and "no opinion" is a different fact from "bad opinion"."""
    v = verdict_from("SomeOtherMint111111111111111111111111111111", None)
    assert v.outcome == "unknown"
    assert v.blocks is False
    assert v.state is None


def test_an_unrecognised_state_refuses_rather_than_guessing() -> None:
    """If Pegana adds a state we have never seen, it is far likelier to be worse than
    PEGGED than better. Refuse and name it."""
    v = verdict_from(MINT, {**LIVE_STALE, "state": "SOMETHING_NEW", "stale": False})
    assert v.outcome == "refuse"
    assert "unrecognised" in v.reason


def test_we_do_not_apply_our_own_discount_cut() -> None:
    """Pegana's schema says the state is CLASS-AWARE and to trust it directly: an LST at
    -1.4% is PEGGED, a fiat stable at far less is DRIFT. A cut of our own would be wrong in
    both directions, so a large discount with a PEGGED state must still approve."""
    lst_like = {**LIVE_STALE, "state": "PEGGED", "stale": False, "discount": "-0.014"}
    assert verdict_from(MINT, lst_like).outcome == "ok"

    fiat_like = {**LIVE_STALE, "state": "DRIFT", "stale": False, "discount": "-0.0009"}
    assert verdict_from(MINT, fiat_like).outcome == "refuse"
