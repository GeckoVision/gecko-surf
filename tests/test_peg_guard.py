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


# --- fail CLOSED on "could not ask", fail open only on a PROVEN "no opinion" ----------
#
# The defect this closes is live, not hypothetical. scripts/pay_with_any_token.py:119
# catches bare Exception and returns verdict_from(mint, None) -> "unknown" -> blocks is
# False. Its own docstring says "Unreachable is `unknown`, never `ok`", which is true and
# operationally meaningless: `unknown` does not stop anything, so an unreachable Pegana
# reads as permission to convert. The gate is decorative exactly when it matters.
#
# `unknown` must stay non-blocking — Pegana tracks a minority of mints and refusing every
# other one would be a blanket denial, not a signal. So the fix is not to make `unknown`
# block; it is to stop "could not ask" from being reported AS `unknown`.


def _reading(**kw):
    from gecko.peg_guard import PegReading

    base = dict(tracked=None, status=None, symbol=None, state_body=None, error=None)
    base.update(kw)
    return PegReading(**base)


def test_an_unreachable_oracle_blocks() -> None:
    from gecko.peg_guard import verdict_from_reading

    v = verdict_from_reading("Mint111", _reading(tracked=None, error="URLError"))
    assert v.outcome == "undetermined"
    assert v.blocks is True


def test_a_tracked_mint_whose_state_read_fails_blocks_and_does_not_lie() -> None:
    from gecko.peg_guard import verdict_from_reading

    v = verdict_from_reading(
        "Mint111",
        _reading(tracked=True, symbol="USDG", state_body=None, error="HTTPError"),
    )
    assert v.outcome == "undetermined"
    assert v.blocks is True
    assert "USDG" in v.reason
    # It must NOT claim the asset is untracked — Pegana tracks it; we failed to read it.
    assert "does not track" not in v.reason


def test_a_genuinely_untracked_mint_still_does_not_block() -> None:
    from gecko.peg_guard import verdict_from_reading

    v = verdict_from_reading("Mint111", _reading(tracked=False, status=404))
    assert v.outcome == "unknown"
    assert v.blocks is False


def test_a_depegged_reading_still_refuses() -> None:
    from gecko.peg_guard import verdict_from_reading

    v = verdict_from_reading(
        "Mint111",
        _reading(tracked=True, symbol="USDG", state_body={"state": "DEPEG"}),
    )
    assert v.outcome == "refuse"
    assert v.blocks is True


def test_blocks_is_a_property_not_a_method() -> None:
    # A bound method is truthy in BOTH directions, so `if v.blocks:` would pass for every
    # verdict ever built and the guard would silently stop guarding.
    from gecko.peg_guard import PegVerdict, verdict_from_reading

    assert isinstance(PegVerdict.__dict__["blocks"], property)
    v = verdict_from_reading("Mint111", _reading(tracked=False, status=404))
    assert isinstance(v.blocks, bool)
    assert callable(v.blocks) is False
