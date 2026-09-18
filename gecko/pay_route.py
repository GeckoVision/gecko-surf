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
  3. the holdings against the price, then each candidate's venue  (chain reads only)

(1) is first because no balance and no swap can make it payable: `make_purchase` pins
classic SPL Token in its IDL, so a Token-2022 priced mint has no path through the program
at all. Discovering that after quoting a swap is how a wallet gets funded three times to
buy from a store that structurally cannot be paid.

A peg gate once sat between (2) and (3): every mint on both sides of the conversion was
read from Pegana's oracle and a bad or unreachable reading refused the route. Pegana
went offline on 2026-09-18 (the project was discontinued), and the gate went with it: a
plan is now made on chain facts alone, and nothing here vouches for any mint's peg. If a
peg oracle returns, it re-enters here as an injected reader, the same seam the old one used.

Control plane: holdings are in-memory pass-through of public chain state. Nothing is
persisted, and no exception message carries a URL or a value.
"""

from __future__ import annotations

from .tools import tool_annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .networks import APPROVABLE_NETWORKS
from .store_accounts import TOKEN_PROGRAM_ID, derive_ata
from .whirlpool_venue import Direction

__all__ = [
    "BLOCKING",
    "Leg",
    "PayOutcome",
    "PayRouteError",
    "SWAP_SLIPPAGE_BPS",
    "PayabilityReport",
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
    "no_route",
]

#: Everything that is not an actionable answer. Stated as a set rather than "not in
#: {payable_now, route_found}" so a new outcome must be classified deliberately.
BLOCKING: frozenset[str] = frozenset(
    {
        "pinned_program_mismatch",
        "self_purchase",
        "no_candidates",
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
    """A sized conversion at one proven venue, and WHICH venue it was.

    ``venue`` and ``curve`` are required and have no defaults, because a route nobody can
    attribute is a route nobody can check. Until 2026-09-13 this carried neither, so the
    report could not name the venue it had chosen and every caller was free to assume
    Orca — which is what the code did.

    ``curve`` is the shape of the liquidity, not a brand: it decides what accounts the
    call needs. A CLMM swap must name tick arrays in the direction of travel; a DLMM one
    names bins; a CPMM pool needs neither. That is why ``tick_spacing`` is optional here
    rather than required — it is a CLMM fact, and demanding it made a Raydium CPMM or
    Meteora DLMM quote literally unrepresentable.
    """

    venue: str
    curve: Curve
    pool: str
    amount_in: int
    direction: Direction
    liquidity: int
    fee_rate: int
    #: CLMM only. ``None`` on curves that have no ticks.
    tick_spacing: int | None = None
    #: The bound ``amount_in`` was sized against. It travels with the number because a
    #: guarantee without its precondition is not a guarantee — a builder that applies a
    #: DIFFERENT bound can now detect the mismatch instead of silently voiding this.
    slippage_bps: int = SWAP_SLIPPAGE_BPS

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "venue": self.venue,
            "curve": self.curve,
            "pool": self.pool,
            "amount_in": str(self.amount_in),
            "direction": self.direction,
            "liquidity": str(self.liquidity),
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
        }
        # Absent, never nulled — the rule `gecko.effects` follows, for the same reason: a
        # null reads as "the answer is nothing", a missing key as "does not apply here".
        if self.tick_spacing is not None:
            out["tick_spacing"] = self.tick_spacing
        return out


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
    #: When the holdings were read. A plan is point-in-time; the conversion happens later.
    holdings_as_of: str
    route: Leg | None = None
    #: Whether PAYING requires a conversion at all — stated outright rather than left
    #: to be inferred from ``route is None``, because the first web session watched an
    #: agent steamroll exactly that inference and swap anyway. None when the question
    #: never arose (refused before holdings were compared).
    conversion_required: bool | None = None
    rejected_legs: tuple[Leg, ...] = ()
    no_pool_for: tuple[str, ...] = ()
    holdings: Mapping[str, int] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.outcome in BLOCKING

    def _next_steps(self) -> list[dict[str, Any]] | None:
        """The ordered tool rail out of this report, with the argument JOINS spelled.

        The joins are non-obvious across tools (``buyer``→``user``,
        ``route.quote.pool``→``pool``) and a cold agent that has to guess them is a
        cold agent that routes around the checked path. ``None`` for a blocked
        report: a refusal's next step is in its reason, never a tool to try anyway.
        """
        if self.outcome == "payable_now":
            return [
                {
                    "tool": "prepare_purchase",
                    "arguments": {
                        "store": self.store_name,
                        "product": self.product,
                        "buyer": self.buyer,
                    },
                    "note": (
                        "no conversion needed — the buyer already holds the priced "
                        "mint. Prepare LATE: preparing starts the ~60-second "
                        "blockhash clock, so have the signer warmed up first."
                    ),
                }
            ]
        if self.outcome == "route_found" and self.route and self.route.quote:
            quote = self.route.quote
            return [
                {
                    "tool": "plan_swap",
                    "arguments": {
                        "user": self.buyer,
                        "input_mint": self.route.held_mint,
                        "output_mint": self.priced_mint,
                        "amount_in": str(quote.amount_in),
                        "pool": quote.pool,
                    },
                    "note": (
                        "the checked conversion this report priced. plan_swap calls "
                        "the buyer `user`; pin `pool` to the one named here so the "
                        "venue that was verified is the venue that executes."
                    ),
                },
                {
                    "tool": "prepare_purchase",
                    "arguments": {
                        "store": self.store_name,
                        "product": self.product,
                        "buyer": self.buyer,
                    },
                    "note": "after the swap settles; same buyer, same store and product.",
                },
            ]
        return None

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
            "holdings_as_of": self.holdings_as_of,
            "conversion_required": self.conversion_required,
            # The execution pointer the first web session lacked: with no breadcrumb at
            # the moment of "how do I run this route", the only swap path carrying
            # instructions was the wallet's own aggregator — which consults no venue
            # check. route_found now names the tool.
            "next_tool": "plan_swap" if self.outcome == "route_found" else None,
            # The FULL rail, for BOTH good outcomes. next_tool above only ever named
            # plan_swap, so payable_now — the most common good outcome — left the agent
            # pointerless one step from success (the exact failure class again, one
            # outcome over). Structured like plan_swap's own next_steps because that is
            # the one format a live web session demonstrably followed to the letter.
            "next_steps": self._next_steps(),
            "route": self.route.to_dict() if self.route else None,
            "rejected_legs": [leg.to_dict() for leg in self.rejected_legs],
            "no_pool_for": list(self.no_pool_for),
            "holdings": {m: str(v) for m, v in self.holdings.items()},
        }


#: (mint) -> the program that owns it. Injected so the whole module runs offline.
MintOwner = Callable[[str], str]
#: Keyword-only venue lookup, so a caller cannot silently swap the pair's order.
#: The shape of a venue's liquidity, which is what decides the accounts a swap needs.
#: Not a brand: two programs with the same curve are called the same way.
Curve = Literal["cpmm", "clmm", "dlmm", "bonding_curve", "aggregator", "orchestrator"]

VenueFinder = Callable[..., Sequence[Any]]


def assess_payment(
    *,
    store: _StoreLike,
    buyer: str,
    holdings: Mapping[str, tuple[int, str]],
    mint_owner: MintOwner,
    find_venues: VenueFinder,
    max_candidates: int = 8,
) -> PayabilityReport:
    """Answer the payability question. Never raises for an ANSWER; see the module docstring."""
    priced_mint = store.mint
    price_raw = int(store.product.price_raw)  # type: ignore[attr-defined]
    product_name = str(store.product.name)  # type: ignore[attr-defined]
    checked_at = datetime.now(UTC).isoformat()
    priced_program = mint_owner(priced_mint)

    conversion_known: bool | None = None

    def report(outcome: PayOutcome, reason: str, **kw: Any) -> PayabilityReport:
        return PayabilityReport(
            outcome=outcome,
            reason=reason,
            conversion_required=kw.pop("conversion_required", conversion_known),
            store_name=store.store_name,
            product=product_name,
            priced_mint=priced_mint,
            price_raw=price_raw,
            pinned_program=TOKEN_PROGRAM_ID,
            priced_program=priced_program,
            buyer=buyer,
            holdings_as_of=checked_at,
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

    # 3. The holdings against the price. Holding enough means NO conversion happens.
    held_priced = holdings.get(priced_mint, (0, TOKEN_PROGRAM_ID))[0]
    conversion_required = held_priced < price_raw
    conversion_known = conversion_required  # noqa: F841 - read by report() via closure
    if not conversion_required:
        return report(
            "payable_now",
            f"the wallet holds {held_priced} of {priced_mint}; the price is {price_raw}.",
        )

    candidates = sorted(
        ((m, amt) for m, (amt, _) in holdings.items() if m != priced_mint and amt > 0),
        key=lambda pair: -pair[1],
    )[:max_candidates]
    if not candidates:
        return report(
            "no_candidates",
            "the wallet holds no other token to convert from.",
        )

    rejected: list[Leg] = []
    no_pool: list[str] = []

    for held_mint, held_raw in candidates:
        venues = find_venues(
            held_mint=held_mint,
            needed_mint=priced_mint,
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
            rejected_legs=tuple(rejected),
            no_pool_for=tuple(no_pool),
        )

    return report(
        "no_route",
        "no proven venue converts anything this wallet holds into the priced mint at a "
        "size the wallet can afford.",
        rejected_legs=tuple(rejected),
        no_pool_for=tuple(no_pool),
    )


# --- the MCP tool ---------------------------------------------------------------------

PLAN_PAYMENT_TOOL: dict[str, Any] = {
    "name": "plan_payment",
    "annotations": tool_annotations(
        read_only=True, open_world=True, title="Plan a payment route"
    ),
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
        "swap fixes it), or the buyer's token account may BE the store's own. Read "
        "`reason` and tell the buyer; do not retry around it. "
        "THE PLAN IS POINT-IN-TIME. It costs nothing and starts no blockhash clock, but "
        "the holdings are as of `holdings_as_of` and the conversion happens later in the "
        "caller's own wallet — re-run before converting. NOTHING HERE VOUCHES FOR A PEG: "
        "no oracle is consulted; whether a stablecoin is on peg is the caller's question "
        "to ask elsewhere before converting into it. "
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
                "enum": sorted(APPROVABLE_NETWORKS),
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
    idl_fetch: Callable[[str], Mapping[str, Any]] | None = None,
    url_guard: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The surface-facing entry: validate, resolve the RPC, answer. Never raises.

    The same two rules the sibling tools on this unauthenticated door enforce: the NETWORK
    is asserted by the caller and never inferred from a URL, and a caller-supplied
    ``rpc_url`` goes through the SSRF guard before anything is fetched. Every transport
    failure comes back redacted to its exception class.
    """
    from .networks import network_for_browse
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

    network, net_error = network_for_browse(args)
    if net_error or network is None:
        return {"error": net_error or "no network"}
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


def _venue_finder(rpc_url: str, rpc_call: Any) -> VenueFinder:
    """Bind the venue search to this call's transport, and size each pool's input.

    The layout is built ONCE from the IDL the caller already fetched, and the seed recipe
    comes from the packaged provider config — so a pool's re-derivation is checked against
    a recipe that shipped with the wheel rather than one the chain proposed.
    """
    from .provider_config import load_packaged_provider
    from .whirlpool_math import size_input_for_output

    bound = validate_swap_bound(SWAP_SLIPPAGE_BPS)
    from .providers.catalog_surface import orquestra_seams
    from .whirlpool_venue import (
        WHIRLPOOL_PROGRAM,
        find_venues as _find,
        whirlpool_layout,
    )

    idl_fetch, _build = orquestra_seams()
    _, apis = load_packaged_provider("orquestra")
    program = apis["whirlpool"].program
    if program is None:  # pragma: no cover - the packaged config always carries it
        raise PayRouteError("the packaged whirlpool config declares no program")
    recipe = dict(program.pdas)["whirlpool"]
    # ONE IDL fetch per program per plan_payment. The finder runs once per candidate
    # mint (up to eight), and an IDL fetch is 1.8 s of network; fetching it inside the
    # loop was most of the wall clock. Memoised per finder, so a new plan_payment still
    # sees a fresh IDL and a drifted one cannot outlive the call that fetched it.
    layouts: dict[str, Any] = {}

    def layout_for(program_id: str) -> Any:
        if program_id not in layouts:
            layouts[program_id] = whirlpool_layout(idl_fetch(program_id))
        return layouts[program_id]

    def finder(*, held_mint: str, needed_mint: str, target_out: int) -> list[Quote]:
        # The IDL is fetched HERE, by the finder that knows which program it needs.
        # Threading a generic `idl` down from `assess_payment` is what welded every
        # route to Orca: the parameter was named for any program and only ever held one.
        layout = layout_for(WHIRLPOOL_PROGRAM)
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
                venue="whirlpool",
                curve="clmm",
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
