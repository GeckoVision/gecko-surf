"""The fork-backed StoreSurface — the semantic runner, executing for real.

This is the seam :mod:`gecko.semantic_runner` declares, filled by the sandbox's
proven-fork loop. It is the ONLY semantic-pack module that can cause a signature,
and it inherits the sandbox's promise unchanged: it acts only against a surfpool
fork that has proved itself over the wire, holds an ephemeral throwaway key that
cannot exist until :func:`~gecko.sandbox.surfnet.prove_surfnet` has done the
round-trip, and never touches mainnet.

The mapping this adds on top of the sandbox: the semantic catalogue speaks
``item_id`` (``"brewed-coffee"``); the on-chain store speaks product NAME
(``"Brewed Coffee (drip)"``). :class:`ForkStoreSurface` binds them through the
catalogue's own ``MenuItem.name``, and reads the authority, price, and mint from
the store's OWN account — never from the catalogue, which is a menu, not an
authorization.

Stock is not a field this program shape carries on-chain, so ``out_of_stock`` is
an explicit staged-state override (scenario 3 needs one item gone). It is named
loudly rather than faked as a chain read: a value we set is not a value we read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gecko.rpc import RpcCall
from gecko.sandbox.rehearse import Rehearsal, rehearse_purchase
from gecko.sandbox.surfnet import EphemeralSigner, SurfnetProof, ephemeral_signer
from gecko.semantic_catalogue import get_item
from gecko.semantic_gate import LiveItemState, ProposedPurchase
from gecko.semantic_grader import PurchaseRecord
from gecko.semantic_runner import SpendResult
from gecko.semantic_seed import read_store_on_fork
from gecko.store_directory import StoreListing


class ForkSurfaceError(Exception):
    """Raised when the fork surface cannot honour the StoreSurface contract."""


def _read_listing(store_address: str, rpc_url: str, rpc_call: RpcCall) -> StoreListing:
    # read_store_on_fork is robust to surfpool's getAccountInfo lag: a store
    # seeded via setAccount shows in getProgramAccounts before getAccountInfo
    # materialises it, so a bare getAccountInfo would false-fail on a fresh seed.
    listing = read_store_on_fork(store_address, rpc_url, rpc_call)
    if listing is None:
        raise ForkSurfaceError(
            f"store account {store_address} does not exist on this fork — seed the "
            "store first (scripts/semantic_seed_fork.py) before running scenarios"
        )
    return listing


@dataclass
class ForkStoreSurface:
    """A StoreSurface over a proven surfpool fork.

    ``proof`` carries the fork binding; every spend derives a FRESH ephemeral
    signer from it (a throwaway buyer per purchase, funded by cheatcode inside
    :func:`rehearse_purchase`). ``store_address`` is the store's on-chain
    account; its authority, prices, and mints are read from it, never supplied.
    """

    proof: SurfnetProof
    store_name: str
    store_address: str
    rpc_call: RpcCall
    out_of_stock: frozenset[str] = field(default=frozenset())
    _listing: StoreListing | None = field(default=None, init=False, repr=False)

    def _listing_now(self) -> StoreListing:
        # Re-read every call: the runner's freshness contract is per-purchase,
        # and a cached price would defeat scenario 3's mid-order price change.
        self._listing = _read_listing(
            self.store_address, self.proof.rpc_url, self.rpc_call
        )
        return self._listing

    def _product_name(self, item_id: str) -> str:
        return get_item(item_id).name  # unknown item_id raises: fail closed

    def authority(self) -> str:
        return self._listing_now().authority

    def read_item(self, item_id: str) -> LiveItemState:
        product_name = self._product_name(item_id)
        listing = self._listing_now()
        product = next((p for p in listing.products if p.name == product_name), None)
        if product is None:
            raise ForkSurfaceError(
                f"item {item_id!r} (product {product_name!r}) is not on the store "
                f"{self.store_name!r} — seed the full catalogue or narrow the scenario"
            )
        return LiveItemState(
            item_id=item_id,
            mint=product.mint,
            price_lamports=product.price_raw,
            in_stock=item_id not in self.out_of_stock,
        )

    def spend(self, proposed: ProposedPurchase) -> SpendResult:
        """Execute one purchase on the fork and report what the LEDGER shows moved.

        The runner has already gated this spend (destination == authority, price
        fresh, in stock, not a duplicate). Here we run the production purchase
        path against the fork and judge by :attr:`Rehearsal.discrepancies` — the
        ledger's own objections — never by the transaction we hoped to send.
        """
        signer: EphemeralSigner = ephemeral_signer(self.proof)
        rehearsal: Rehearsal = rehearse_purchase(
            self.proof,
            buyer=signer,
            store=self.store_name,
            product=self._product_name(proposed.item_id),
            rpc_call=self.rpc_call,
        )
        if not rehearsal.landed or rehearsal.discrepancies:
            # An unlanded run, or one the ledger objected to (price mismatch,
            # unbalanced debit/credit, a receipt the program never wrote), is
            # not a purchase. The runner turns this into a named block.
            return SpendResult(landed=False, purchase=None)

        outflow = rehearsal.buyer_sol.moved if rehearsal.buyer_sol is not None else None
        if outflow is None:
            # Landed and balanced but the payer's delta was not tracked: fail
            # closed rather than grade a spend whose cost is unknown.
            return SpendResult(landed=False, purchase=None)

        # A balanced rehearsal proved the store's authority-derived token account
        # rose by exactly the price — so the credited party IS the authority.
        return SpendResult(
            landed=True,
            purchase=PurchaseRecord(
                mint=rehearsal.mint,
                lamports_paid=abs(outflow),
                destination=self.authority(),
            ),
        )
