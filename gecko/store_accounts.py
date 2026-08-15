"""A store NAME resolved to the accounts a purchase must carry — from the chain, together.

WHY THIS MODULE EXISTS, and it is a measured failure rather than a tidy-up. On mainnet,
``--store geckocoffee`` told the program one store while handing it another store's
accounts: the store name reached the instruction argument, and ``receipts``, ``authority``
and the recipient token account stayed pinned to the constants of the store the script was
first written for. The chain caught it — ``AnchorError ... account: receipts ...
ConstraintSeeds`` (2006), left ``H7Bj…`` (what was passed), right ``HVkb…`` (what the
program derived from the name) — and that catch was luck, not design. A flag that selects
HALF a store reads as a working control and fails at the last possible moment.

So the unit here is the STORE, not the field. :class:`StoreAccounts` is the only way to
carry these addresses, it is frozen, and its constructor re-derives what it can:
``receipts`` must be ``PDA(['receipts', store_name])`` and ``token_account`` must be
``ATA(authority, mint)``. That is the program's own ConstraintSeeds check, run offline,
before anything is built — a half-selected store is not merely refused downstream, it
cannot be constructed.

WHAT IS READ FROM CHAIN AND WHY IT HAS TO BE. Only ``receipts`` is derivable from a name.
The AUTHORITY (who gets paid) and the MINT (what a product is priced in) are written
INSIDE the store's account, so a path that reads no chain state can only guess them, and a
guessed authority builds a purchase that pays the wrong person. This module takes the one
targeted read — ``getAccountInfo`` on the derived PDA, never ``getProgramAccounts`` — and
decodes it with :func:`gecko.store_directory.decode_store`, whose bounds-checked cursor
already treats every byte as hostile.

WHAT IS TRUSTED. The account CONTENTS come from one unauthenticated node, which is the
same trust position as the simulation that follows and strictly weaker than a wired
constant. Three things narrow it: the address we ask for is DERIVED here from the name, the
node's ``owner`` field must be the let_me_buy program, and the decoded ``store_name`` must
be the name we asked for. A node that answers with some other account's bytes is refused,
not believed. Nothing here authorizes anything — the purchase still goes through the plan
refusal, the simulation, the binding and the spend policy before a signature exists.

Control plane only: names, prices and public account addresses — the on-chain equivalent of
a catalog page. Nothing is persisted, no balance is read, no buyer data is decoded.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from .pda import PdaNode, derive_pda

# The SAME packaged ProgramSpec the hosted pre-flight derives from, imported rather than
# re-loaded so the seed recipe has one source. A second copy of "what seeds the receipts
# PDA" is how the offline check and the program eventually disagree.
from .prepare_purchase import _program_spec
from .rpc import RpcCall, default_rpc_call
from .store_directory import (
    LET_ME_BUY_PROGRAM_ID,
    StoreDecodeError,
    StoreProduct,
    decode_store,
)

__all__ = [
    "ProductNotOnMenu",
    "ResolvedStore",
    "StoreAccounts",
    "StoreAccountsMismatch",
    "StoreNotFound",
    "StoreResolutionError",
    "allowed_destinations",
    "derive_ata",
    "purchase_accounts",
    "purchase_args",
    "receipts_pda",
    "resolve_store",
]


class StoreResolutionError(Exception):
    """A store name could not be turned into accounts that belong together."""


class StoreNotFound(StoreResolutionError):
    """No storefront by that name on that network. Absence refuses; it never defaults."""


class ProductNotOnMenu(StoreResolutionError):
    """That store does not list that product, so there is no price and no mint."""


class StoreAccountsMismatch(StoreResolutionError):
    """These addresses do not belong to the store they are labelled with.

    The offline twin of the program's ``ConstraintSeeds`` — raised where it is still free,
    instead of on chain where it costs a fee.
    """


def derive_ata(owner: str, mint: str, *, token_program: str = TOKEN_PROGRAM_ID) -> str:
    """The associated token account for ``owner`` and ``mint`` — a PDA, so derived.

    THE single implementation. It was copied into two scripts, which is how a purchase
    ends up derived one way and verified another; every caller in the repo imports this.
    """
    from solders.pubkey import Pubkey

    address, _bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(owner)),
            bytes(Pubkey.from_string(token_program)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID),
    )
    return str(address)


@lru_cache(maxsize=1)
def _receipts_recipe() -> PdaNode:
    """The packaged seed recipe for ``receipts``. Cached: it is a file read of a constant."""
    return _program_spec().pdas["receipts"]


def receipts_pda(store_name: str) -> str:
    """The store's one account, derived from its name alone — no chain read, no guess."""
    return derive_pda(_receipts_recipe(), {"store_name": store_name}).address


@dataclass(frozen=True)
class StoreAccounts:
    """One store's accounts FOR ONE PRODUCT, and they are checked to belong together.

    Construction is the guard: ``receipts`` must be the PDA of ``store_name`` and
    ``token_account`` must be the authority's ATA for the product's mint. Mixing two
    stores' addresses raises here, offline, rather than reverting on chain.
    """

    store_name: str
    product: StoreProduct
    receipts: str
    authority: str
    token_account: str

    def __post_init__(self) -> None:
        derived = receipts_pda(self.store_name)
        if self.receipts != derived:
            raise StoreAccountsMismatch(
                f"receipts {self.receipts} is not the store account for "
                f"{self.store_name!r} — that name derives {derived}. These are two "
                "different stores; naming one and paying the other is the exact fault "
                "the program refuses with ConstraintSeeds (2006)."
            )
        expected = derive_ata(self.authority, self.product.mint)
        if self.token_account != expected:
            raise StoreAccountsMismatch(
                f"token_account {self.token_account} is not "
                f"ATA({self.authority}, {self.product.mint}) — that pair derives "
                f"{expected}. The credited account must belong to this store's authority."
            )

    @property
    def mint(self) -> str:
        """The mint this product is priced in, as the store's own account declares it."""
        return self.product.mint


@dataclass(frozen=True)
class ResolvedStore:
    """A storefront read from its own account: who it pays, and what it sells."""

    store_name: str
    receipts: str
    authority: str
    products: tuple[StoreProduct, ...]

    def accounts_for(self, product_name: str) -> StoreAccounts:
        """The accounts for one product on this menu.

        :raises ProductNotOnMenu: naming the store, the product and what IS listed — a
            refusal you cannot act on is a shrug, and the mint (hence the credited token
            account) is a per-product fact that an unlisted product simply does not have.
        """
        needle = product_name.strip().lower()
        for entry in self.products:
            if entry.name.lower() == needle:
                return StoreAccounts(
                    store_name=self.store_name,
                    product=entry,
                    receipts=self.receipts,
                    authority=self.authority,
                    token_account=derive_ata(self.authority, entry.mint),
                )
        listed = ", ".join(entry.name for entry in self.products) or "nothing"
        raise ProductNotOnMenu(
            f"{self.store_name!r} does not sell {product_name!r} — it lists {listed}. "
            "The price and the mint are per-product facts written in the store's own "
            "account, so an unlisted product has neither. "
            "THE MENU IS THE GROUND TRUTH AND THE CHOICE IS NOT MINE TO MAKE: if exactly "
            "one of those is plainly what the buyer meant, call again with its EXACT "
            "name; if more than one could be, ASK THE BUYER WHICH — do not pick for them, "
            "and do not pick the first that looks close."
        )

    def close_matches(self, product_name: str) -> tuple[StoreProduct, ...]:
        """Listed products whose name overlaps ``product_name`` as a substring, either way.

        Deliberately LEXICAL and nothing more. "a black coffee" resolves to Espresso by
        knowing what black coffee IS — world knowledge the calling agent has and a store
        account does not — so inferring it here would be guessing with the merchant's
        money. What this catches is the narrower, honest case: a typo or a partial name
        (``Espress`` -> ``Espresso``), where the overlap is evidence rather than opinion.

        It is a HINT, never a selection: even a single match still refuses, because a tool
        that quietly substitutes the one thing it found is a tool that eventually buys the
        wrong thing confidently.
        """
        needle = product_name.strip().lower()
        if not needle:
            return ()
        return tuple(
            entry
            for entry in self.products
            if needle in entry.name.lower() or entry.name.lower() in needle
        )


def resolve_store(
    store_name: str, *, rpc_url: str, rpc_call: RpcCall | None = None
) -> ResolvedStore:
    """Read the store named ``store_name`` off the chain at ``rpc_url``.

    One targeted ``getAccountInfo`` on the PDA this name derives — not a program scan, so
    it works on nodes that disable ``getProgramAccounts``, and the address asked for is
    the address the name entails.

    :raises StoreNotFound: the account does not exist, or is not owned by let_me_buy.
    :raises StoreAccountsMismatch: the account exists but names a different store.
    :raises StoreResolutionError: the bytes do not decode as a storefront.
    """
    name = store_name.strip()
    if not name:
        raise StoreNotFound(
            "name the store to buy from — an empty name resolves to no account, and "
            "nothing here substitutes a default store for one you did not name"
        )
    address = receipts_pda(name)
    call = rpc_call or default_rpc_call
    response = call(rpc_url, "getAccountInfo", [address, {"encoding": "base64"}])
    value = (response.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise StoreNotFound(
            f"no store named {name!r} on this network: its receipts account {address} "
            "does not exist. Absence is a refusal, never a fallback to another store."
        )
    owner = value.get("owner")
    if owner != LET_ME_BUY_PROGRAM_ID:
        raise StoreNotFound(
            f"{address} exists but is owned by {owner!r}, not the let_me_buy program "
            f"({LET_ME_BUY_PROGRAM_ID}) — it is not a storefront"
        )
    try:
        raw = base64.b64decode(_account_data(value))
        listing = decode_store(raw, address=address)
    except (StoreDecodeError, TypeError, ValueError) as exc:
        raise StoreResolutionError(
            f"the account at {address} did not decode as a let_me_buy storefront: {exc}"
        ) from exc
    if listing.store_name != name:
        # The address was derived from `name`, so a different name inside it means the
        # node answered with something other than what was asked for. Never believed.
        raise StoreAccountsMismatch(
            f"the account at {address} — derived from {name!r} — calls itself "
            f"{listing.store_name!r}. Refusing rather than believing one of the two."
        )
    return ResolvedStore(
        store_name=listing.store_name,
        receipts=address,
        authority=listing.authority,
        products=listing.products,
    )


def _account_data(value: dict[str, Any]) -> str:
    """The base64 payload of a ``getAccountInfo`` value. Untrusted shape, so it is checked."""
    data = value.get("data")
    if isinstance(data, list) and data and isinstance(data[0], str):
        return data[0]
    if isinstance(data, str):  # some nodes answer base64 without the encoding tag
        return data
    raise ValueError("the node returned no base64 account data")


def purchase_accounts(
    store: StoreAccounts,
    *,
    buyer: str,
    sender: str | None = None,
    recipient: str | None = None,
) -> dict[str, str]:
    """The resolved ``make_purchase`` account map, in the IDL's order.

    Every store-side address comes from ONE :class:`StoreAccounts`, so the three that used
    to be constants cannot disagree with the store name any more. ``sender``/``recipient``
    override the derived defaults for the demos that deliberately bind a wrong account (a
    self-paying purchase, a wallet passed where a token account belongs) — the plan
    refusal and the simulation are what judge those, which is the point of being able to
    build them.
    """
    return {
        "receipts": store.receipts,
        "signer": buyer,
        "authority": store.authority,
        "mint": store.mint,
        "sender_token_account": (
            derive_ata(buyer, store.mint) if sender is None else sender
        ),
        "recipient_token_account": (
            store.token_account if recipient is None else recipient
        ),
        "token_program": TOKEN_PROGRAM_ID,
        "system_program": SYSTEM_PROGRAM_ID,
        "associated_token_program": ASSOCIATED_TOKEN_PROGRAM_ID,
    }


def purchase_args(store: StoreAccounts, *, table: int) -> dict[str, Any]:
    """The instruction arguments, from the same resolved store as the accounts.

    Built here rather than at each call site so ``store_name`` and the account map are one
    decision: they diverged once, and the program's seeds constraint was the only thing
    that noticed.
    """
    return {
        "store_name": store.store_name,
        "product_name": store.product.name,
        "table_number": table,
    }


def allowed_destinations(store: StoreAccounts, *, buyer_ata: str) -> frozenset[str]:
    """The addresses this purchase may write to — this store's, plus the buyer's own.

    Derived from the same resolved store as the plan, because a spend policy naming
    another store's accounts is a policy that permits the wrong payee while refusing the
    right one.
    """
    return frozenset({store.receipts, store.authority, store.token_account, buyer_ata})
