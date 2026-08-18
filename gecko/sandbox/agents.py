"""Two roles over one storefront — and the seam where a model would go.

A WAITER takes an order and buys; a KITCHEN marks it delivered. Two agents cooperating is
not the achievement here and no part of this module claims it is: any framework does that.
What this shape is for is the moment the two disagree about whether an order exists, which
is a moment the program produces on its own, silently, past twenty open receipts.

WHAT IS ACTUALLY IN HERE, which is deliberately almost nothing. Every judgement these
roles make already belongs to the package and is CALLED rather than restated:

* the menu, the price and the mint come from :func:`gecko.store_accounts.resolve_store` —
  read from the store's own account, never supplied;
* the refusal to guess a product comes from :class:`~gecko.store_accounts.ProductNotOnMenu`
  and :meth:`~gecko.store_accounts.ResolvedStore.close_matches`, whose whole design is that
  even a single lexical near-miss still refuses;
* the purchase is :func:`gecko.sandbox.rehearse.rehearse_purchase`, judged by what MOVED;
* the delivery is :func:`gecko.sandbox.deliver.rehearse_delivery`, judged by reading the
  row back.

So the roles are thin on purpose. They are the two decision SEAMS — "which product did
the customer mean" and "which order do we mark now" — expressed as injected callables, so
the same run is deterministic today (:func:`exact_name_only`, :func:`oldest_first`) and can
be handed to a model later without moving a single fact about the chain into the model.

THE SEAM DOES NOT GET TO INVENT. A waiter policy returns a name and that name is still
resolved against the store's own account, so a model that answers with a product the shop
does not sell is refused by the chain's data rather than believed. A kitchen policy returns
receipt ids and those ids must be on tickets the kitchen already holds, so a model cannot
mark an order nobody placed. Both guards are cheap, and both close the failure that makes
an LLM in a spending path frightening.

WHY THE KITCHEN'S TICKETS ARE NOT THE CHAIN'S ROWS. :class:`Ticket` is the VENUE's own
memory of an order it took. The receipts vec is not that: it holds at most
:data:`RECEIPTS_CAP` rows and ``make_purchase`` drops the oldest to make room, with no
error, no event and no log. A venue that treats the vec as its queue therefore loses
orders it genuinely took, and a venue that keeps its own queue and marks them delivered
gets ``Ok`` for orders that are no longer there. Keeping the two types apart is what lets
:class:`Confirmation` say which of them is right.

Control-plane invariant #1 holds: :class:`ResidentRow` carries a receipt id and a delivery
flag and nothing else. The vec has no index, so reading any row means walking every row —
and every other buyer's bytes are compared and dropped inside this module rather than
returned from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ..rpc import RpcCall, default_rpc_call
from ..store_accounts import (
    ProductNotOnMenu,
    ResolvedStore,
    receipts_pda,
    resolve_store,
)
from ..store_directory import StoreDecodeError, StoreProduct
from .deliver import Delivery, rehearse_delivery
from .rehearse import (
    DEFAULT_FEE_LAMPORTS,
    Rehearsal,
    _account,
    _raw_data,
    _walk_receipts,
    rehearse_purchase,
)
from .surfnet import EphemeralSigner, SandboxError, SurfnetProof

__all__ = [
    "RECEIPTS_CAP",
    "AgentError",
    "ChainQueue",
    "Confirmation",
    "Kitchen",
    "KitchenPolicy",
    "Order",
    "OrderRefused",
    "ResidentRow",
    "Ticket",
    "Waiter",
    "WaiterPolicy",
    "exact_name_only",
    "oldest_first",
    "read_chain_queue",
]

#: How many receipts the account can hold. MEASURED, and recorded in the program notes of
#: ``gecko/providers/configs/orquestra/let_me_buy.json``: the vec is capped at 20, the
#: account never reallocs (fixed 3,681 bytes), and at the cap ``make_purchase`` performs
#: ``receipts.remove(0)``. Nothing in the IDL says any of this.
RECEIPTS_CAP = 20


class AgentError(SandboxError):
    """A role was asked for something it may not decide on its own.

    Raised where continuing would mean acting on a value nobody established: a store whose
    account cannot be read, or a policy naming an order the venue never took.
    """


# ---------------------------------------------------------------------------
# what the chain holds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResidentRow:
    """One row of the receipts vec, reduced to what a delivery queue may know.

    An id and a flag. The buyer, the price, the table and the product name of somebody
    else's order are read during the walk — the vec is sequential — and dropped before
    anything leaves :func:`read_chain_queue`.
    """

    receipt_id: int
    delivered: bool


@dataclass(frozen=True)
class ChainQueue:
    """The store's receipts vec at one moment, beside the counter that outgrew it.

    :attr:`total_purchases` is the program's own count of every sale it ever recorded;
    ``len(rows)`` is how many it still has. The gap between them is not an estimate and not
    an inference from logs — there are no logs — it is simply the arithmetic the program
    leaves behind after evicting.
    """

    rows: tuple[ResidentRow, ...]
    total_purchases: int
    account_bytes: int

    @property
    def at_cap(self) -> bool:
        """Is the next purchase going to evict? The whole demonstration needs this True."""
        return len(self.rows) >= RECEIPTS_CAP

    @property
    def oldest(self) -> ResidentRow | None:
        """The row ``receipts.remove(0)`` takes next, or ``None`` on an empty vec."""
        return self.rows[0] if self.rows else None

    @property
    def evicted(self) -> int:
        """Receipts this store recorded and no longer holds. Gone from chain, not archived."""
        return max(self.total_purchases - len(self.rows), 0)


def read_chain_queue(
    store: str, *, rpc_url: str, rpc_call: RpcCall | None = None
) -> ChainQueue:
    """Read the store's own account and reduce the vec to ids and delivery flags.

    One targeted ``getAccountInfo`` on the address the NAME derives — the same read
    :func:`gecko.store_accounts.resolve_store` performs, never a program scan.

    :raises AgentError: if the account cannot be read or does not decode, because every
        number below it would otherwise be a guess about somebody's order book.
    """
    call = rpc_call or default_rpc_call
    address = receipts_pda(store)
    raw = _raw_data(_account(call, rpc_url, address))
    if raw is None:
        raise AgentError(
            f"the store account {address} for {store!r} could not be read from {rpc_url}"
        )
    try:
        rows, total = _walk_receipts(raw)
    except StoreDecodeError as exc:
        raise AgentError(
            f"the receipts vec at {address} did not decode ({exc}) — refusing to report a "
            "queue read out of bytes whose layout is unknown"
        ) from exc
    return ChainQueue(
        rows=tuple(
            ResidentRow(int(row["receipt_id"]), bool(row["delivered"])) for row in rows
        ),
        total_purchases=total,
        account_bytes=len(raw),
    )


# ---------------------------------------------------------------------------
# the waiter
# ---------------------------------------------------------------------------

#: Given what the customer said and the menu the store's account declares, return the
#: EXACT listed name they meant — or ``None`` to abstain, which is not a failure. A model
#: goes here later; nothing about the chain does.
WaiterPolicy = Callable[[str, tuple[StoreProduct, ...]], str | None]


def exact_name_only(request: str, menu: tuple[StoreProduct, ...]) -> str | None:
    """The scripted policy: resolve a request only when it already IS a listed name.

    Deterministic, offline, and the same answer forever — which is what a demo and a CI run
    both need, and what a model could not promise on a path that spends money. Everything
    it cannot answer it abstains on, and the abstention becomes a refusal that names the
    menu. "A coffee" against a shop selling three coffees is exactly that case: bridging it
    needs to know what a coffee IS, which is world knowledge the caller has and a store
    account does not.
    """
    needle = request.strip().lower()
    for entry in menu:
        if entry.name.lower() == needle:
            return entry.name
    return None


@dataclass(frozen=True)
class Order:
    """A request resolved to ONE listed product, at the store's own declared price."""

    request: str
    product: str
    price_ui: str
    price_raw: int
    mint: str


@dataclass(frozen=True)
class OrderRefused:
    """The waiter would not choose, and said what the choice is between.

    :attr:`candidates` is the whole menu — ground truth from the store's account.
    :attr:`close_matches` is lexical evidence of a typo or a partial name and is never a
    selection: a category word matches nothing there, which is the signal that the request
    needs a mind rather than a string comparison.
    """

    request: str
    reason: str
    candidates: tuple[str, ...]
    close_matches: tuple[str, ...]


@dataclass(frozen=True)
class Waiter:
    """Reads the store, refuses to guess, and buys exactly what was named."""

    proof: SurfnetProof
    store: str
    #: The decision seam. Swap for a model without moving any chain fact into it.
    decide: WaiterPolicy = exact_name_only
    rpc_call: RpcCall | None = None

    def menu(self) -> ResolvedStore:
        """The storefront as its own account declares it: who it pays, and what it sells."""
        return resolve_store(
            self.store, rpc_url=self.proof.rpc_url, rpc_call=self.rpc_call
        )

    def take_order(
        self, request: str, *, menu: ResolvedStore | None = None
    ) -> Order | OrderRefused:
        """Turn what the customer said into one listed product, or refuse and name why.

        The policy's answer is not trusted: whatever it returns is resolved against the
        store's own account, so a name the shop does not sell refuses here — with the
        package's own words — instead of becoming a purchase of the nearest thing.
        """
        listing = menu if menu is not None else self.menu()
        chosen = self.decide(request, listing.products)
        wanted = chosen if chosen is not None else request
        try:
            accounts = listing.accounts_for(wanted)
        except ProductNotOnMenu as exc:
            return OrderRefused(
                request=request,
                reason=str(exc),
                candidates=tuple(entry.name for entry in listing.products),
                close_matches=tuple(
                    entry.name for entry in listing.close_matches(wanted)
                ),
            )
        product = accounts.product
        return Order(
            request=request,
            product=product.name,
            price_ui=product.price_ui,
            price_raw=product.price_raw,
            mint=product.mint,
        )

    def buy(
        self,
        order: Order,
        *,
        buyer: EphemeralSigner,
        table_number: int = 0,
        fee_lamports: int = DEFAULT_FEE_LAMPORTS,
    ) -> Rehearsal:
        """Fund, prepare through the production path, sign, land, and judge what moved.

        On a fork the rehearsal IS the purchase — one signed transaction, not a dry run
        followed by a real one. There is no second buy anywhere in this module.
        """
        return rehearse_purchase(
            self.proof,
            buyer=buyer,
            store=self.store,
            product=order.product,
            table_number=table_number,
            fee_lamports=fee_lamports,
            rpc_call=self.rpc_call,
        )


# ---------------------------------------------------------------------------
# the kitchen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """An order the VENUE took, in the venue's own memory. Not a row of the vec.

    The distinction is the demonstration: a ticket outlives the eviction that removes its
    row, which is precisely why the kitchen can be asked to deliver an order the chain no
    longer has.
    """

    receipt_id: int
    product: str


#: Given the tickets the venue holds, return the receipt ids to mark delivered now. A model
#: goes here later — and may only name ids that are already on a ticket.
KitchenPolicy = Callable[[tuple[Ticket, ...]], Sequence[int]]


def oldest_first(tickets: tuple[Ticket, ...]) -> Sequence[int]:
    """The scripted policy: every open ticket, oldest id first. A queue, in other words."""
    return [ticket.receipt_id for ticket in sorted(tickets, key=lambda t: t.receipt_id)]


@dataclass(frozen=True)
class Confirmation:
    """One delivery, read two ways — and they are allowed to disagree.

    :attr:`trusted` is what a queue that believes a confirmed transaction concludes.
    :attr:`read_back` is what the store's own bytes say afterwards. A venue that never
    compares the two cannot tell the difference between a delivered order and an evicted
    one, because from every other vantage point they are identical.
    """

    ticket: Ticket
    delivery: Delivery

    @property
    def trusted(self) -> bool:
        """Confirmed signature, fee charged, no error. The whole of the naive check."""
        return self.delivery.landed

    @property
    def read_back(self) -> bool | None:
        """Did the row go from not-delivered to delivered? ``None`` when nothing landed."""
        return self.delivery.delivered

    @property
    def queues_disagree(self) -> bool:
        """THE FINDING, per order: the chain said Ok and the row does not agree."""
        return self.trusted and self.read_back is not True


@dataclass(frozen=True)
class Kitchen:
    """Marks tickets delivered — and checks the row came back changed."""

    proof: SurfnetProof
    store: str
    #: The decision seam: which of the venue's open tickets to mark now.
    decide: KitchenPolicy = oldest_first
    rpc_call: RpcCall | None = None

    def pick(self, tickets: Sequence[Ticket]) -> tuple[Ticket, ...]:
        """Ask the policy which tickets to work, and refuse an id nobody ordered.

        :raises AgentError: if the policy names a receipt id the kitchen does not hold.
            A delivery is a write, and a seam that may later hold a model must not be able
            to reach an order the venue never took.
        """
        held = {ticket.receipt_id: ticket for ticket in tickets}
        chosen = tuple(self.decide(tuple(tickets)))
        unknown = sorted(set(chosen) - set(held))
        if unknown:
            raise AgentError(
                f"the kitchen policy named receipt id(s) {unknown}, which are on no ticket "
                f"this kitchen holds ({sorted(held)}) — refusing to mark an order nobody "
                "placed"
            )
        return tuple(held[receipt_id] for receipt_id in chosen)

    def confirm(
        self,
        ticket: Ticket,
        *,
        authority: EphemeralSigner,
        take_the_store: bool = True,
    ) -> Confirmation:
        """Land ``mark_as_delivered`` for one ticket and READ THE ROW BACK.

        ``take_the_store`` rewrites the store's ``authority`` field to ``authority`` with a
        surfpool cheatcode first — a fork-only act with no counterpart on any real chain,
        and a faithful model of the merchant's own kitchen signing. It is documented at
        :func:`gecko.sandbox.deliver.rehearse_delivery`, including the measured fact that
        the program does not actually require it.
        """
        delivery = rehearse_delivery(
            self.proof,
            authority=authority,
            store=self.store,
            receipt_id=ticket.receipt_id,
            take_the_store=take_the_store,
            rpc_call=self.rpc_call,
        )
        return Confirmation(ticket=ticket, delivery=delivery)
