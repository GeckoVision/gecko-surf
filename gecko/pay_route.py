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
    "SWAP_SLIPPAGE_BPS",
    "PayabilityReport",
    "PegCheck",
    "Quote",
    "assess_payment",
    "validate_swap_bound",
]

#: Token-2022. Held balances live under it; a let_me_buy PRICE never can.
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

#: The slippage bound the swap is SIZED against, and it must equal the one the swap is
#: BUILT with (``scripts/prepare_whirlpool_swap.py --slippage-bps``, default 100).
#:
#: This was two numbers and that was the bug. `size_input_for_output(target, ..., B)`
#: returns the smallest input whose guaranteed floor AT B clears the target; sizing at 50
#: and building at 100 means the floor actually enforced is strictly lower than the one
#: the size was chosen for, so the guarantee is void. On mainnet it printed 101,001
#: against a 100,000 price where 101,022 was needed and cleared with 1,011 to spare —
#: the FILL rescued it, which is precisely the failure mode a guarantee is supposed to
#: remove. `whirlpool_math` records the same 21-unit shortfall in its own docstring.
#:
#: A test pins this against the builder's declared default, so the two cannot drift apart
#: again silently.
SWAP_SLIPPAGE_BPS = 100

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


def validate_swap_bound(bps: int) -> int:
    """Refuse a nonsense slippage bound, ONCE, before any venue is looked up.

    Deliberately not inside the per-venue loop. Swallowed there it degrades into
    ``no_route``, which tells a caller who misconfigured a floor that nobody trades their
    pair — a configuration error wearing a market answer's clothes. ``>= 10_000`` is
    "accept any price", which is the ABSENCE of a bound rather than a loose one.
    """
    if not isinstance(bps, int) or isinstance(bps, bool):
        raise PayRouteError(f"slippage bound must be an int, got {type(bps).__name__}")
    if not 0 <= bps < 10_000:
        raise PayRouteError(
            f"slippage bound {bps} is not in [0, 10000) — 10000 or more accepts any "
            "price at all, which is no bound"
        )
    return bps


class _StoreLike(Protocol):
    """The slice of ``StoreAccounts`` this module consumes.

    Every member is read-only: the concrete type is a FROZEN dataclass whose
    ``__post_init__`` is the guard that its accounts belong together, and a protocol
    declaring settable attributes would refuse it.
    """

    @property
    def store_name(self) -> str: ...
    @property
    def authority(self) -> str: ...
    @property
    def token_account(self) -> str: ...
    @property
    def mint(self) -> str: ...
    @property
    def product(self) -> Any: ...


@dataclass(frozen=True)
class Quote:
    """A sized conversion at one proven venue."""

    pool: str
    amount_in: int
    direction: Direction
    liquidity: int
    tick_spacing: int
    fee_rate: int
    #: The bound ``amount_in`` was sized against. It travels with the number because a
    #: guarantee without its precondition is not a guarantee — a builder that applies a
    #: DIFFERENT bound can now detect the mismatch instead of silently voiding this.
    slippage_bps: int = SWAP_SLIPPAGE_BPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "amount_in": str(self.amount_in),
            "direction": self.direction,
            "liquidity": str(self.liquidity),
            "tick_spacing": self.tick_spacing,
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
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

        venues = find_venues(
            held_mint=held_mint,
            needed_mint=priced_mint,
            idl=idl,
            target_out=price_raw - held_priced,
        )
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


# --- the MCP tool ---------------------------------------------------------------------

PLAN_PAYMENT_TOOL: dict[str, Any] = {
    "name": "plan_payment",
    "description": (
        "Answer 'can this wallet buy this product, and if not what is the shortest "
        "CHECKED route' — in one call, without signing anything or building any bytes. "
        "Reads the store's price and mint from its own on-chain account, reads the "
        "buyer's holdings under BOTH token programs, and if the two do not match derives "
        "the venue that converts one into the other, making each candidate pool "
        "re-derive its own address before it is offered. A pool that cannot is DROPPED, "
        "not ranked lower — the second-best answer here is a real funded pool that would "
        "take the money and report success. "
        "IT CAN REFUSE, AND A REFUSAL IS THE ANSWER. `blocked: true` means do not "
        "proceed: the product may be priced in a mint let_me_buy structurally cannot "
        "debit (its IDL pins classic SPL Token, so a Token-2022 price has no path and no "
        "swap fixes it), the buyer's token account may BE the store's own, or a peg "
        "verdict may block. Read `reason` and tell the buyer; do not retry around it. "
        "PEG EVIDENCE IS POINT-IN-TIME. It costs nothing and starts no blockhash clock, "
        "but the verdicts are as of `peg_evidence_as_of` and the conversion happens later "
        "in the caller's own wallet — re-run before converting. A mint whose oracle could "
        "not be REACHED blocks: silence is not consent. A mint the oracle provably does "
        "not track does not block, and is reported as unknown. "
        "`peg_checks` covers every mint evaluated INCLUDING the destination. "
        "LIMITS, stated rather than discovered: a route is a pointer, not an executed "
        "swap; sizing uses the pool's spot price and models no price impact; `no_route` "
        "means no PROVEN venue was affordable, not that none exists. Read-only; nothing "
        "here holds a key."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "store": {"type": "string", "description": "the storefront name"},
            "product": {"type": "string", "description": "the product as listed"},
            "buyer": {
                "type": "string",
                "description": "the wallet that would pay — its holdings are what is checked",
            },
            "network": {
                "type": "string",
                "description": "mainnet (default) or a fork you name with rpc_url",
            },
            "rpc_url": {
                "type": "string",
                "description": "your own node; requires `network` so the two cannot disagree",
            },
        },
        "required": ["store", "product", "buyer"],
    },
}

#: Base58 has no 0, O, I or l.
_B58_CHARS = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def _is_pubkey(value: Any) -> bool:
    return (
        isinstance(value, str) and 32 <= len(value) <= 44 and set(value) <= _B58_CHARS
    )


def plan_payment_result(
    arguments: Any,
    *,
    rpc_call: Any = None,
    peg_reader: PegReader | None = None,
    idl_fetch: Callable[[str], Mapping[str, Any]] | None = None,
    url_guard: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The surface-facing entry: validate, resolve the RPC, answer. Never raises.

    The same two rules the sibling tools on this unauthenticated door enforce: the NETWORK
    is asserted by the caller and never inferred from a URL, and a caller-supplied
    ``rpc_url`` goes through the SSRF guard before anything is fetched. Every transport
    failure comes back redacted to its exception class.
    """
    from .networks import UNKNOWN_NETWORK, coerce_network
    from .prepare_purchase import _resolve_rpc_url
    from .rpc import default_rpc_call
    from .store_accounts import resolve_store

    args = arguments or {}
    store_name = args.get("store")
    product = args.get("product")
    buyer = args.get("buyer")
    if not isinstance(store_name, str) or not store_name.strip():
        return {"error": "`store` is required — the storefront name to read."}
    if not isinstance(product, str) or not product.strip():
        return {"error": "`product` is required — the product as the store lists it."}
    if not _is_pubkey(buyer):
        return {
            "error": (
                "`buyer` must be a base58 account address — its holdings are the whole "
                "question, so there is nothing to answer without one."
            )
        }

    network = coerce_network(args.get("network"))
    if network == UNKNOWN_NETWORK:
        supplied = args.get("rpc_url")
        if isinstance(supplied, str) and supplied.strip():
            return {
                "error": (
                    "you named an `rpc_url` but not a `network`. Which chain that node "
                    "answers for cannot be read from its hostname, so say mainnet, "
                    "devnet, testnet or fork."
                )
            }
        network = "mainnet"  # type: ignore[assignment]
    # ``url_guard`` mirrors `prepare_purchase_result`'s seam, and exists for the same
    # reason: the DEFAULT refuses loopback because this is an unauthenticated door, but a
    # rehearsal runs against the operator's own fork, which lives at 127.0.0.1 by
    # definition. Without the seam the tool's fork rung was unreachable even in-process.
    rpc_url, refusal = _resolve_rpc_url(args.get("rpc_url"), network, url_guard)
    if refusal or rpc_url is None:
        return {"error": refusal or "no RPC url"}

    call = rpc_call or default_rpc_call
    try:
        resolved = resolve_store(store_name.strip(), rpc_url=rpc_url, rpc_call=call)
        accounts = resolved.accounts_for(product.strip())
        report = assess_payment(
            store=accounts,
            buyer=str(buyer),
            holdings=read_holdings(rpc_url, str(buyer), rpc_call=call),
            mint_owner=lambda mint: read_mint_owner(rpc_url, mint, rpc_call=call),
            peg_reader=peg_reader or _default_peg_reader(),
            idl_fetch=idl_fetch or _default_idl_fetch(),
            find_venues=_venue_finder(rpc_url, call),
        )
    except Exception as exc:  # noqa: BLE001 - redacted to a class at the transport edge
        return {"error": f"{type(exc).__name__}: {exc}"}

    out = report.to_dict()
    out["network"] = network
    return out


def read_holdings(
    rpc_url: str, owner: str, *, rpc_call: Any
) -> dict[str, tuple[int, str]]:
    """Every token this wallet holds: mint -> (raw amount, token program).

    Asked of BOTH token programs, because ``getTokenAccountsByOwner`` filters by one and
    a Token-2022 balance is invisible to a classic-SPL query. That asymmetry is the whole
    reason this module exists.
    """
    out: dict[str, tuple[int, str]] = {}
    for program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        rows = (
            rpc_call(
                rpc_url,
                "getTokenAccountsByOwner",
                [owner, {"programId": program}, {"encoding": "jsonParsed"}],
            ).get("result")
            or {}
        ).get("value") or []
        for row in rows:
            info = (
                ((row.get("account") or {}).get("data") or {}).get("parsed") or {}
            ).get("info") or {}
            amount = int((info.get("tokenAmount") or {}).get("amount") or 0)
            mint = info.get("mint")
            if amount > 0 and mint:
                out[mint] = (amount, program)
    return out


def read_mint_owner(rpc_url: str, mint: str, *, rpc_call: Any) -> str:
    """The program that owns a mint, read from the mint account — never inferred."""
    value = (
        rpc_call(rpc_url, "getAccountInfo", [mint, {"encoding": "base64"}]).get(
            "result"
        )
        or {}
    ).get("value")
    if not value or not value.get("owner"):
        raise PayRouteError(f"mint {mint} does not exist on this network")
    return str(value["owner"])


def _default_peg_reader() -> PegReader:
    from .pegana import pegana_reader

    return pegana_reader()


def _default_idl_fetch() -> Callable[[str], Mapping[str, Any]]:
    from .providers.catalog_surface import orquestra_seams
    from .whirlpool_venue import WHIRLPOOL_PROGRAM

    idl_fetch, _build = orquestra_seams()
    return lambda _name: idl_fetch(WHIRLPOOL_PROGRAM)


def _venue_finder(rpc_url: str, rpc_call: Any) -> VenueFinder:
    """Bind the venue search to this call's transport, and size each pool's input.

    The layout is built ONCE from the IDL the caller already fetched, and the seed recipe
    comes from the packaged provider config — so a pool's re-derivation is checked against
    a recipe that shipped with the wheel rather than one the chain proposed.
    """
    from .provider_config import load_packaged_provider
    from .whirlpool_math import size_input_for_output

    bound = validate_swap_bound(SWAP_SLIPPAGE_BPS)
    from .whirlpool_venue import (
        find_venues as _find,
        whirlpool_layout,
    )

    _, apis = load_packaged_provider("orquestra")
    program = apis["whirlpool"].program
    if program is None:  # pragma: no cover - the packaged config always carries it
        raise PayRouteError("the packaged whirlpool config declares no program")
    recipe = dict(program.pdas)["whirlpool"]

    def finder(
        *, held_mint: str, needed_mint: str, idl: Mapping[str, Any], target_out: int
    ) -> list[Quote]:
        layout = whirlpool_layout(idl)
        venues = _find(
            rpc_url,
            held_mint,
            needed_mint,
            layout=layout,
            recipe=recipe,
            rpc_call=rpc_call,
        )
        return [
            Quote(
                pool=v.pool,
                amount_in=size_input_for_output(
                    target_out,
                    v.sqrt_price,
                    v.fee_rate,
                    a_to_b=v.direction == "a_to_b",
                    slippage_bps=bound,
                ),
                direction=v.direction,
                liquidity=v.liquidity,
                tick_spacing=v.tick_spacing,
                fee_rate=v.fee_rate,
                slippage_bps=bound,
            )
            for v in venues
        ]

    return finder
