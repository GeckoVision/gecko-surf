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

Outcome = Literal["ok", "refuse", "unknown"]

#: (symbol) -> the parsed `/v1/assets/{symbol}/state` body, or None when untracked.
StateReader = Callable[[str], "dict[str, Any] | None"]


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
        """Only ``refuse`` stops a conversion. ``unknown`` is reported, never enforced —
        Pegana tracks 67 assets, so treating every other mint as suspect would make this
        guard a blanket denial rather than a signal."""
        return self.outcome == "refuse"


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
