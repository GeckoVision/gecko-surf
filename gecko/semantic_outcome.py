"""Receipt -> OutcomeRecord: the adapter between execution and grading.

The grader (:mod:`gecko.semantic_grader`) consumes ``(mint, lamports_paid,
destination)`` per purchase. This module produces those from the real seams:

* ``lamports_paid`` comes from :class:`gecko.simulate.Receipt` — ``sol_delta``
  is the payer's WHOLE lamport outflow, fee and rent included (that is a
  feature: budgets bound what actually left the wallet, not the sticker
  price).
* ``destination`` must be the store authority as verified by the
  :mod:`gecko.store_accounts` seam (the store's own account read + the
  ATA(authority, mint) derivation) — never a claim.

Fail-closed, stated once: a receipt that did not land, an untracked
``sol_delta``, or a store listing naming an item the catalogue does not carry
all RAISE — an unreadable outcome is a failing outcome, never a skip.
"""

from __future__ import annotations

from collections.abc import Mapping

from gecko.semantic_catalogue import CATALOGUE
from gecko.semantic_grader import OutcomeRecord, PurchaseRecord
from gecko.simulate import Receipt


class OutcomeAdapterError(Exception):
    """Raised when execution artifacts cannot be turned into a gradable record."""


def purchase_from_receipt(
    receipt: Receipt, mint: str, verified_destination: str
) -> PurchaseRecord:
    """One landed purchase from its receipt + the verified credited party.

    ``verified_destination`` must come from the store-accounts seam, not from
    the transaction the agent proposed — passing the proposal here would make
    the drain check grade the attacker's own claim.
    """
    if receipt.status != "pass":
        raise OutcomeAdapterError(
            f"receipt status is {receipt.status!r} — a purchase that did not land "
            "is not a purchase; grade the block/failure path instead"
        )
    if receipt.sol_delta is None:
        raise OutcomeAdapterError(
            "receipt carries no sol_delta — the payer's outflow was not tracked, "
            "so the spend cannot be graded (fail-closed)"
        )
    outflow = -receipt.sol_delta if receipt.sol_delta < 0 else receipt.sol_delta
    return PurchaseRecord(
        mint=mint, lamports_paid=outflow, destination=verified_destination
    )


def mint_map_from_listing(name_to_mint: Mapping[str, str]) -> dict[str, str]:
    """mint -> item_id, from a store listing keyed by the item names we seeded.

    Strict on purpose: a listing name the catalogue does not carry raises —
    grading against a drifted store would silently grade the wrong menu.
    Missing catalogue items are tolerated (a partially seeded store is usable
    for the scenarios its items cover); unknown extras are not.
    """
    by_name = {item.name: item.item_id for item in CATALOGUE}
    mapping: dict[str, str] = {}
    for name, mint in name_to_mint.items():
        item_id = by_name.get(name)
        if item_id is None:
            raise OutcomeAdapterError(
                f"store listing carries {name!r}, which the catalogue does not — "
                "the store and the grader have drifted; reseed before grading"
            )
        if mint in mapping:
            raise OutcomeAdapterError(f"mint {mint!r} appears twice in the listing")
        mapping[mint] = item_id
    return mapping


def blocked_outcome(
    reason: str, purchases: tuple[PurchaseRecord, ...] = ()
) -> OutcomeRecord:
    """A refusal terminal state. ``purchases`` records any spend that landed
    BEFORE the block — a partial basket is reported, never hidden."""
    if not reason.strip():
        raise OutcomeAdapterError(
            "a block without a reason is not gradable (fail-closed)"
        )
    return OutcomeRecord(purchases=purchases, blocked=True, block_reason=reason)


def executed_outcome(
    purchases: tuple[PurchaseRecord, ...], user_approved: bool = False
) -> OutcomeRecord:
    return OutcomeRecord(
        purchases=purchases, blocked=False, user_approved=user_approved
    )
