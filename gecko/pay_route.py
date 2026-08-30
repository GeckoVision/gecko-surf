"""Can this wallet buy this product — and if not, what is the shortest CHECKED route?

A storefront prices a product in ONE mint under ONE token program. A buyer holds whatever
they hold. Those two facts are read from chain and compared; nothing here guesses, signs,
builds transaction bytes, or starts a blockhash clock.

WHY IT IS A REFUSAL AND NOT A ROUTER. Asked "I only have USDG but I want a coffee priced
in USDC", the lexical router returns `let_me_buy.make_purchase` — it finds the DESTINATION
and misses the conversion, because there is no cross-program edge expressing "convert
first". So this does not pretend to have discovered a route by search. It states a fact
the chain makes certain — your mint cannot pay this price — and then derives the one venue
that changes that, making the venue prove itself.

THE ORDER OF THE CHECKS IS THE DESIGN. Each refusal below is cheaper and more certain than
the one after it, and a check that runs too late is a check that has already spent
something:

  1. the priced mint's token program vs the one let_me_buy PINS   (no I/O at all)
  2. self-purchase                                                (no I/O at all)
  3. the DESTINATION mint's peg                                   (one oracle read)
  4. each candidate's peg, before its venue is looked up          (oracle before RPC)

(1) is first because no balance and no swap can make it payable: `make_purchase` pins
classic SPL Token in its IDL, so a Token-2022 priced mint has no path through the program
at all. Discovering that after quoting a swap is how a wallet gets funded three times to
buy from a store that structurally cannot be paid.

(3) exists because the thesis is symmetric. Checking only what you SELL and not what you
BUY quotes a route into a broken peg and reports it as fine.

Control plane: peg bodies and holdings are in-memory pass-through of public chain and
oracle state. Nothing is persisted, and no exception message carries a URL or a value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .peg_guard import PegReader, PegVerdict, verdict_from_reading
from .store_accounts import TOKEN_PROGRAM_ID, derive_ata
from .whirlpool_venue import Direction

__all__ = [
    "BLOCKING",
    "Leg",
    "PayOutcome",
    "PayRouteError",
    "PayabilityReport",
    "PegCheck",
    "Quote",
    "assess_payment",
]

#: Token-2022. Held balances live under it; a let_me_buy PRICE never can.
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

PayOutcome = Literal[
    "payable_now",
    "route_found",
    "pinned_program_mismatch",
    "self_purchase",
    "no_candidates",
    "peg_blocked",
    "no_route",
]

#: Everything that is not an actionable answer. Stated as a set rather than "not in
#: {payable_now, route_found}" so a new outcome must be classified deliberately.
BLOCKING: frozenset[str] = frozenset(
    {
        "pinned_program_mismatch",
        "self_purchase",
        "no_candidates",
        "peg_blocked",
        "no_route",
    }
)


class PayRouteError(Exception):
    """A payability question we cannot answer at all — never a refusal."""


class _StoreLike(Protocol):
    """The slice of ``StoreAccounts`` this module consumes."""

    store_name: str
    authority: str
    token_account: str

    @property
    def mint(self) -> str: ...


@dataclass(frozen=True)
class Quote:
    """A sized conversion at one proven venue."""

    pool: str
    amount_in: int
    direction: Direction
    liquidity: int
    tick_spacing: int
    fee_rate: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "amount_in": str(self.amount_in),
            "direction": self.direction,
            "liquidity": str(self.liquidity),
            "tick_spacing": self.tick_spacing,
            "fee_rate": self.fee_rate,
        }


@dataclass(frozen=True)
class Leg:
    """One candidate conversion — taken, or rejected with the reason."""

    held_mint: str
    held_raw: int
    quote: Quote | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "held_mint": self.held_mint,
            "held_raw": str(self.held_raw),
            "quote": self.quote.to_dict() if self.quote else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PegCheck:
    """One mint's peg verdict, and which side of the conversion it sits on."""

    mint: str
    side: Literal["destination", "candidate"]
    outcome: str
    blocks: bool
    reason: str

    @classmethod
    def of(cls, verdict: PegVerdict, side: str) -> "PegCheck":
        return cls(
            mint=verdict.mint,
            side=side,  # type: ignore[arg-type]
            outcome=verdict.outcome,
            blocks=verdict.blocks,
            reason=verdict.reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "side": self.side,
            "outcome": self.outcome,
            "blocks": self.blocks,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PayabilityReport:
    """The whole answer, including the facts gathered before a short-circuit."""

    outcome: PayOutcome
    reason: str
    store_name: str
    product: str
    priced_mint: str
    price_raw: int
    pinned_program: str
    priced_program: str
    buyer: str
    peg_evidence_as_of: str
    route: Leg | None = None
    peg_checks: tuple[PegCheck, ...] = ()
    rejected_legs: tuple[Leg, ...] = ()
    no_pool_for: tuple[str, ...] = ()
    holdings: Mapping[str, int] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.outcome in BLOCKING

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "blocked": self.blocks,
            "reason": self.reason,
            "store": self.store_name,
            "product": self.product,
            "priced_mint": self.priced_mint,
            "price_raw": str(self.price_raw),
            "pinned_program": self.pinned_program,
            "priced_program": self.priced_program,
            "buyer": self.buyer,
            "peg_evidence_as_of": self.peg_evidence_as_of,
            "route": self.route.to_dict() if self.route else None,
            "peg_checks": [c.to_dict() for c in self.peg_checks],
            "rejected_legs": [leg.to_dict() for leg in self.rejected_legs],
            "no_pool_for": list(self.no_pool_for),
            "holdings": {m: str(v) for m, v in self.holdings.items()},
        }


#: (mint) -> the program that owns it. Injected so the whole module runs offline.
MintOwner = Callable[[str], str]
#: Keyword-only venue lookup, so a caller cannot silently swap the pair's order.
VenueFinder = Callable[..., Sequence[Any]]


def assess_payment(
    *,
    store: _StoreLike,
    buyer: str,
    holdings: Mapping[str, tuple[int, str]],
    mint_owner: MintOwner,
    peg_reader: PegReader,
    idl_fetch: Callable[[str], Mapping[str, Any]],
    find_venues: VenueFinder,
    max_candidates: int = 8,
) -> PayabilityReport:
    """Answer the payability question. Never raises for an ANSWER; see the module docstring."""
    priced_mint = store.mint
    price_raw = int(store.product.price_raw)  # type: ignore[attr-defined]
    product_name = str(store.product.name)  # type: ignore[attr-defined]
    checked_at = datetime.now(UTC).isoformat()
    priced_program = mint_owner(priced_mint)

    def report(outcome: PayOutcome, reason: str, **kw: Any) -> PayabilityReport:
        return PayabilityReport(
            outcome=outcome,
            reason=reason,
            store_name=store.store_name,
            product=product_name,
            priced_mint=priced_mint,
            price_raw=price_raw,
            pinned_program=TOKEN_PROGRAM_ID,
            priced_program=priced_program,
            buyer=buyer,
            peg_evidence_as_of=checked_at,
            holdings={m: amt for m, (amt, _) in holdings.items()},
            **kw,
        )

    # 1. The pin. No balance and no swap makes this payable, so nothing else is worth
    #    reading — not the oracle, not the IDL, not a pool.
    if priced_program != TOKEN_PROGRAM_ID:
        return report(
            "pinned_program_mismatch",
            (
                f"{product_name} is priced in {priced_mint}, owned by {priced_program}, "
                f"but let_me_buy PINS token_program to {TOKEN_PROGRAM_ID} in its IDL. "
                "make_purchase has no path for this mint — a swap cannot fix it, because "
                "the destination itself is unspendable through this program."
            ),
        )

    # 2. Self-purchase. BOTH addresses derive with the pinned classic program, because the
    #    store's own token_account does — putting them on different bases would make this
    #    comparison silently never fire.
    buyer_ata = derive_ata(buyer, priced_mint, token_program=TOKEN_PROGRAM_ID)
    if buyer_ata == store.token_account:
        return report(
            "self_purchase",
            (
                f"the buyer's token account for {priced_mint} IS the store's own "
                f"({buyer_ata}). This purchase would pay you back and still emit a "
                "PurchaseMade event, which is a settled sale that moved nothing."
            ),
        )

    # 3. The DESTINATION's peg, always recorded.
    checks: list[PegCheck] = [
        PegCheck.of(
            verdict_from_reading(priced_mint, peg_reader(priced_mint)), "destination"
        )
    ]
    held_priced = holdings.get(priced_mint, (0, TOKEN_PROGRAM_ID))[0]

    # 4. Holding enough means NO conversion happens, so the destination peg is
    #    information rather than a refusal — nobody is being asked to acquire the asset.
    if held_priced >= price_raw:
        return report(
            "payable_now",
            f"the wallet holds {held_priced} of {priced_mint}; the price is {price_raw}.",
            peg_checks=tuple(checks),
        )

    # 5. A conversion INTO a broken peg is refused, whatever the wallet holds.
    if checks[0].blocks:
        return report(
            "peg_blocked",
            (
                f"a conversion would end in {priced_mint}, and its peg cannot be relied "
                f"on: {checks[0].reason}"
            ),
            peg_checks=tuple(checks),
        )

    candidates = sorted(
        ((m, amt) for m, (amt, _) in holdings.items() if m != priced_mint and amt > 0),
        key=lambda pair: -pair[1],
    )[:max_candidates]
    if not candidates:
        return report(
            "no_candidates",
            "the wallet holds no other token to convert from.",
            peg_checks=tuple(checks),
        )

    idl = idl_fetch("whirlpool")
    rejected: list[Leg] = []
    no_pool: list[str] = []
    peg_refused = 0

    for held_mint, held_raw in candidates:
        verdict = verdict_from_reading(held_mint, peg_reader(held_mint))
        checks.append(PegCheck.of(verdict, "candidate"))
        if verdict.blocks:
            # Skip THIS mint, not the wallet — another holding may be sound.
            peg_refused += 1
            rejected.append(Leg(held_mint, held_raw, None, verdict.reason))
            continue

        venues = find_venues(held_mint=held_mint, needed_mint=priced_mint, idl=idl)
        if not venues:
            no_pool.append(held_mint)
            continue

        quote = venues[0]
        if quote.amount_in > held_raw:
            rejected.append(
                Leg(
                    held_mint,
                    held_raw,
                    quote,
                    f"the swap needs {quote.amount_in} and the wallet holds {held_raw}.",
                )
            )
            continue
        return report(
            "route_found",
            (
                f"convert {quote.amount_in} of {held_mint} into {priced_mint} at pool "
                f"{quote.pool}, then purchase."
            ),
            route=Leg(held_mint, held_raw, quote),
            peg_checks=tuple(checks),
            rejected_legs=tuple(rejected),
            no_pool_for=tuple(no_pool),
        )

    if peg_refused and peg_refused == len(candidates):
        return report(
            "peg_blocked",
            "every mint this wallet could convert from has a peg verdict that blocks.",
            peg_checks=tuple(checks),
            rejected_legs=tuple(rejected),
            no_pool_for=tuple(no_pool),
        )
    return report(
        "no_route",
        "no proven venue converts anything this wallet holds into the priced mint at a "
        "size the wallet can afford.",
        peg_checks=tuple(checks),
        rejected_legs=tuple(rejected),
        no_pool_for=tuple(no_pool),
    )
