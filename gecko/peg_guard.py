"""Is this asset holding its peg? A guard on converting, not a step in converting.

WHY THIS IS A GUARD AND NOT A STEP. A peg reading changes nothing when the asset is fine
— you convert either way. It earns its place only when it REFUSES: "do not turn USDG into
USDC right now, you would eat the discount." So the only interesting outcome is the one
that stops something, and the design question is what to do when the answer is not a clean
yes.

WE DO NOT IMPOSE OUR OWN THRESHOLD, AND THE PROVIDER TOLD US NOT TO. Pegana's `PegState`
schema says it outright: *"The state is CLASS-AWARE: an LST reading a -1.4% discount
(normal unstaking spread) is PEGGED, while a fiat stable at far less would be DRIFT. Trust
this value directly rather than imposing a naive discount cut."* An LST at -1.4% is healthy;
a fiat stable at -0.5% is not. A discount cut we invented would be wrong in both directions,
so `state` is read and believed. That is a DECLARED cross-surface vocabulary — the provider
defines the semantics, we honour them rather than re-deriving them badly.

THREE OUTCOMES, BECAUSE "NO DATA" IS NOT "GOOD DATA". `gecko/score.py` already models this
split (a measured zero is a result; an unmeasurable one is `undetermined`) and this module
follows it:

* ``ok``      — tracked, fresh, and PEGGED. Convert.
* ``refuse``  — tracked and NOT holding: DRIFT/DEPEG/CRITICAL/BLACK_SWAN, or the reading is
                STALE, or the state is UNKNOWN. We cannot say the peg holds, so we do not.
* ``unknown`` — Pegana does not track this mint at all. No opinion exists; that is a
                different fact from a bad one, and collapsing them would either block every
                untracked asset or quietly wave through a broken one.

STALE IS A REFUSAL, NOT AN APPROVAL. `state: UNKNOWN, stale: true, state_reason:
"stale_source"` is what USDG actually returned while this was written. Treating that as
"fine" is the same fail-open shape as a spend cap that stops applying when it cannot read
the amount — the control silently ceases to exist at exactly the moment it might matter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

__all__ = ["PegVerdict", "PEG_STATES", "HOLDING", "verdict_from", "Outcome"]

#: Pegana's own enum, mirrored so an unrecognised value is visibly unrecognised rather
#: than silently falling into whichever branch happens to catch it.
PEG_STATES: frozenset[str] = frozenset(
    {"PEGGED", "DRIFT", "DEPEG", "CRITICAL", "BLACK_SWAN", "UNKNOWN"}
)
#: The ONLY state that means "convert freely". Everything else is a reason to stop, and
#: that asymmetry is deliberate: this is a guard.
HOLDING: frozenset[str] = frozenset({"PEGGED"})

#: Four answers, and the fourth is the one that makes this a guard.
#:
#:   ok           — Pegana was asked, and says the peg is holding.
#:   refuse       — Pegana was asked, and says it is not (or cannot vouch for it now).
#:   unknown      — Pegana was asked and PROVABLY has no opinion: it does not track this
#:                  mint. Reported, never enforced.
#:   undetermined — Pegana could not be asked, or tracks the asset and could not be read.
#:
#: `unknown` and `undetermined` are the distinction the old three-value vocabulary could
#: not make, and collapsing them is what let an unreachable oracle read as permission.
#: Named `PegOutcome` rather than `Outcome` because `ingest_gate.Outcome` is a different
#: vocabulary and one of them had to say which it was.
PegOutcome = Literal["ok", "refuse", "unknown", "undetermined"]

#: Backwards-compatible alias for the pure judge's original three-value vocabulary.
Outcome = PegOutcome

#: A reading is BLOCKING when it is a refusal or when there is no reading at all. Silence
#: from an oracle is not consent: the money is about to move on the strength of an
#: opinion nobody actually obtained.
_BLOCKING: frozenset[str] = frozenset({"refuse", "undetermined"})

#: (symbol) -> the parsed `/v1/assets/{symbol}/state` body, or None when untracked.
StateReader = Callable[[str], "dict[str, Any] | None"]

#: (mint) -> what the oracle actually said. The injected seam for every caller that needs
#: a peg opinion: `gecko.pegana.pegana_reader` live, `recorded_peg_reader` at $0.
PegReader = Callable[[str], "PegReading"]


@dataclass(frozen=True)
class PegReading:
    """What actually came back from the oracle, BEFORE any judgement of the peg.

    The split matters. `verdict_from` judges a peg from a state body and cannot express
    "I never got one" — its `body is None` means "Pegana does not track this mint", which
    is a claim about Pegana, not about our network. A reading carries the difference.

    ``tracked`` is deliberately tri-state and is established from the HTTP STATUS, never
    from the shape of a 200 body:

      True  — Pegana tracks this asset (a card came back and validated).
      False — Pegana answered 404. The ONLY proof that no opinion exists.
      None  — we could not ask, or could not understand the answer.

    Reading `tracked=False` off a body shape is what turns every degraded 200 — `{}`, a
    WAF envelope, a rate-limit served as 200, a schema change that wraps the card — into
    a forged "no opinion", which is a fail-open wearing a fix's clothes.
    """

    tracked: bool | None
    status: int | None = None
    symbol: str | None = None
    state_body: "dict[str, Any] | None" = None
    #: Exception CLASS name only. Never a URL, a body, or a token.
    error: str | None = None


@dataclass(frozen=True)
class PegVerdict:
    """What Pegana says about one asset, and whether that is a reason to stop."""

    mint: str
    symbol: str | None
    outcome: Outcome
    state: str | None
    stale: bool
    discount: str | None
    reason: str

    @property
    def blocks(self) -> bool:
        """``refuse`` and ``undetermined`` stop a conversion; ``ok`` and ``unknown`` do not.

        ``unknown`` stays non-blocking on purpose — Pegana tracks a minority of mints, so
        refusing every other one would be a blanket denial rather than a signal. But that
        leniency is only defensible when "no opinion" has been PROVEN (a 404). An oracle
        we could not reach has not told us anything, and the old vocabulary reported that
        silence as ``unknown`` — which is why an unreachable Pegana read as permission.

        A property, not a method: a bound method is truthy in both directions, so
        ``if verdict.blocks:`` would fire for every verdict ever built and the guard would
        stop guarding without failing a single test."""
        return self.outcome in _BLOCKING


def verdict_from(
    mint: str, body: dict[str, Any] | None, symbol: str | None = None
) -> PegVerdict:
    """Judge one asset from Pegana's own state reading. Pure; the fetch is the caller's.

    ``body`` is the `/v1/assets/{symbol}/state` payload, or ``None`` when Pegana does not
    track this mint.
    """
    if body is None:
        return PegVerdict(
            mint=mint,
            symbol=symbol,
            outcome="unknown",
            state=None,
            stale=False,
            discount=None,
            reason="Pegana does not track this mint — no peg opinion exists for it",
        )

    state = body.get("state")
    stale = bool(body.get("stale"))
    discount = body.get("discount")
    sym = symbol or body.get("asset")

    # Staleness is checked BEFORE the state value: a PEGGED reading from a stale source
    # is a statement about the past, and the whole point of a guard is the present.
    if stale:
        return PegVerdict(
            mint=mint,
            symbol=sym,
            outcome="refuse",
            state=state,
            stale=True,
            discount=discount,
            reason=(
                f"Pegana's reading is STALE ({body.get('state_reason') or 'stale'}), last "
                f"updated {body.get('updated_at')} — it cannot vouch for the peg right now"
            ),
        )

    if state in HOLDING:
        return PegVerdict(
            mint=mint,
            symbol=sym,
            outcome="ok",
            state=state,
            stale=False,
            discount=discount,
            reason=f"{sym} is {state} (discount {discount})",
        )

    if state == "UNKNOWN":
        return PegVerdict(
            mint=mint,
            symbol=sym,
            outcome="refuse",
            state=state,
            stale=False,
            discount=discount,
            reason=f"Pegana reports {sym} as UNKNOWN — it cannot say the peg holds",
        )

    if state in PEG_STATES:
        return PegVerdict(
            mint=mint,
            symbol=sym,
            outcome="refuse",
            state=state,
            stale=False,
            discount=discount,
            reason=(
                f"{sym} is {state} (discount {discount}) — converting now realises that "
                "discount. Pegana's state is CLASS-AWARE, so this is its judgement for "
                "this asset class, not a threshold we applied"
            ),
        )

    # An enum value we have never seen. Refuse rather than guess which side it falls on —
    # a new state is far more likely to be worse than PEGGED than better.
    return PegVerdict(
        mint=mint,
        symbol=sym,
        outcome="refuse",
        state=state,
        stale=stale,
        discount=discount,
        reason=(
            f"Pegana returned an unrecognised state {state!r}; this build knows "
            f"{sorted(PEG_STATES)}. Refusing rather than assuming it is benign"
        ),
    )


def verdict_from_reading(mint: str, reading: PegReading) -> PegVerdict:
    """Judge one asset from a `PegReading` — the fetch-aware wrapper around `verdict_from`.

    Four branches, and the first two are the whole point of the type:

      tracked is None                     -> undetermined (BLOCKS). We could not ask.
      tracked is True, state_body is None -> undetermined (BLOCKS). Pegana tracks it and
                                             we could not read its state. The reason names
                                             the symbol and must NOT claim it is untracked.
      tracked is False                    -> delegate to `verdict_from(mint, None)`, which
                                             is `unknown` and does not block.
      tracked is True, state_body present -> delegate to the pure judge, unchanged.
    """
    if reading.tracked is None:
        detail = f" ({reading.error})" if reading.error else ""
        status = f" [HTTP {reading.status}]" if reading.status is not None else ""
        return PegVerdict(
            mint=mint,
            symbol=reading.symbol,
            outcome="undetermined",
            state=None,
            stale=False,
            discount=None,
            reason=(
                f"could not obtain a peg reading{detail}{status} — silence is not consent, "
                "so this conversion is refused rather than assumed safe"
            ),
        )

    if reading.tracked and reading.state_body is None:
        detail = f" ({reading.error})" if reading.error else ""
        name = reading.symbol or mint
        return PegVerdict(
            mint=mint,
            symbol=reading.symbol,
            outcome="undetermined",
            state=None,
            stale=False,
            discount=None,
            reason=(
                f"Pegana tracks {name} but its peg state could not be read{detail} — "
                "refused rather than assumed safe"
            ),
        )

    if not reading.tracked:
        return verdict_from(mint, None, reading.symbol)

    return verdict_from(mint, reading.state_body, reading.symbol)
