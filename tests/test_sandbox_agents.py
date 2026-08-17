"""The two roles, offline — the refusal, the two guards, and the queues disagreeing.

Every leg here runs against the fake fork that MODELS the program (``StoreFork``, imported
from the delivery suite rather than re-written): on ``sendTransaction`` it scans the vec,
flips the row if it is there, and returns a happy signature either way. That is the whole
reason an offline test can tell a delivery from a silence — a fake that only recorded calls
could not, and neither could the assertion.

The fork-marked end-to-end is ``scripts/two_agent_showcase.py``, which drives a real
surfpool mainnet fork. Nothing here reaches a validator or mainnet.
"""

from __future__ import annotations

import dataclasses


import pytest

from gecko.sandbox.agents import (
    RECEIPTS_CAP,
    AgentError,
    Kitchen,
    Order,
    OrderRefused,
    ResidentRow,
    Ticket,
    Waiter,
    exact_name_only,
    oldest_first,
    read_chain_queue,
)
from gecko.sandbox.surfnet import ephemeral_signer
from gecko.store_accounts import StoreNotFound, receipts_pda
from gecko.store_directory import StoreProduct
from scripts.two_agent_showcase import deferring_store_reset

from test_sandbox_deliver import StoreFork, store_bytes
from test_sandbox_rehearse import fresh_pubkey, offline_proof

STORE = "teststore"


def wired(rows: list[tuple[int, str, bool]], *, total: int = 128) -> StoreFork:
    """A storefront holding ``rows``, with the program's own account layout."""
    return StoreFork(
        "http://127.0.0.1:9997",
        store_name=STORE,
        raw=store_bytes(
            store_name=STORE, authority=fresh_pubkey(), rows=rows, total=total
        ),
    )


def full_vec() -> list[tuple[int, str, bool]]:
    """Twenty resident rows, ids 107..126 — the measured shape of a store at the cap."""
    return [(receipt_id, fresh_pubkey(), False) for receipt_id in range(107, 127)]


# ---------------------------------------------------------------------------
# the waiter refuses, and names what the choice is between
# ---------------------------------------------------------------------------


def test_an_ambiguous_request_is_refused_with_the_menu_named() -> None:
    """The least dramatic moment and the most important one: it does not pick."""
    fork = wired([(126, fresh_pubkey(), False)])
    waiter = Waiter(proof=offline_proof(fork.rpc_url), store=STORE, rpc_call=fork)

    refused = waiter.take_order("something to drink")

    assert isinstance(refused, OrderRefused)
    assert refused.candidates == ("Water",), "the candidates are the store's own menu"
    assert refused.close_matches == (), (
        "a category is not a typo and must not look like one"
    )
    assert "does not sell" in refused.reason


def test_a_policy_that_invents_a_product_is_refused_by_the_stores_own_account() -> None:
    """THE SEAM DOES NOT GET TO INVENT — the guard that matters once a model sits in it."""
    fork = wired([(126, fresh_pubkey(), False)])
    waiter = Waiter(
        proof=offline_proof(fork.rpc_url),
        store=STORE,
        rpc_call=fork,
        decide=lambda _request, _menu: "Negroni",
    )

    refused = waiter.take_order("a black coffee")

    assert isinstance(refused, OrderRefused)
    assert "Negroni" in refused.reason
    assert refused.candidates == ("Water",)


def test_an_exact_listed_name_becomes_an_order_at_the_stores_own_price() -> None:
    """The price is READ, never supplied — nothing in the order came from the caller."""
    fork = wired([(126, fresh_pubkey(), False)])
    waiter = Waiter(proof=offline_proof(fork.rpc_url), store=STORE, rpc_call=fork)

    order = waiter.take_order("water")

    assert isinstance(order, Order)
    assert (order.product, order.price_raw, order.price_ui) == ("Water", 100_000, "0.1")


def test_the_scripted_policy_abstains_rather_than_ranking() -> None:
    """It answers only when the request already IS a listed name. No nearest-match."""
    products = (
        StoreProduct(name="Espresso", price_raw=1, decimals=6, mint="M"),
        StoreProduct(name="Mochaccino", price_raw=2, decimals=6, mint="M"),
    )
    assert exact_name_only("espresso", products) == "Espresso"
    assert exact_name_only("a coffee", products) is None
    assert exact_name_only("Espress", products) is None


# ---------------------------------------------------------------------------
# what the chain holds, and what it stopped holding
# ---------------------------------------------------------------------------


def test_the_queue_reports_the_cap_and_the_receipts_that_are_gone() -> None:
    """20 resident against 128 recorded is not an estimate — it is what is left."""
    fork = wired(full_vec(), total=128)

    queue = read_chain_queue(STORE, rpc_url=fork.rpc_url, rpc_call=fork)

    assert len(queue.rows) == RECEIPTS_CAP and queue.at_cap
    assert queue.oldest == ResidentRow(107, False)
    assert queue.evicted == 108, "128 recorded, 20 held — the rest are gone from chain"


def test_a_row_that_leaves_the_walk_carries_no_buyer() -> None:
    """Invariant #1, as a shape: the walk sees every row and only two fields survive it."""
    assert [field.name for field in dataclasses.fields(ResidentRow)] == [
        "receipt_id",
        "delivered",
    ]


# ---------------------------------------------------------------------------
# the kitchen marks, and reads the row back
# ---------------------------------------------------------------------------


def test_the_kitchen_will_not_mark_an_order_nobody_placed() -> None:
    """The other half of "the seam does not get to invent" — no write from a made-up id."""
    fork = wired(full_vec())
    kitchen = Kitchen(
        proof=offline_proof(fork.rpc_url),
        store=STORE,
        rpc_call=fork,
        decide=lambda _tickets: [999],
    )

    with pytest.raises(AgentError, match="999"):
        kitchen.pick([Ticket(107, "Water")])
    assert fork.landed == [], "a refused pick must not have reached the program"


def test_the_policy_picks_and_the_kitchen_keeps_the_order() -> None:
    fork = wired(full_vec())
    kitchen = Kitchen(proof=offline_proof(fork.rpc_url), store=STORE, rpc_call=fork)
    tickets = (Ticket(126, "Water"), Ticket(107, "Water"))

    assert [ticket.receipt_id for ticket in kitchen.pick(tickets)] == [107, 126]
    assert list(oldest_first(tickets)) == [107, 126]


def test_an_evicted_order_is_marked_delivered_and_the_two_queues_disagree() -> None:
    """THE DEMONSTRATION, offline: the transaction succeeds and the row is not there.

    A queue that reads the confirmation records a delivery; a queue that reads the row back
    knows the order is still open. Both look at the same signature.
    """
    resident = full_vec()
    fork = wired(resident)
    proof = offline_proof(fork.rpc_url)
    kitchen = Kitchen(proof=proof, store=STORE, rpc_call=fork)

    evicted = kitchen.confirm(
        Ticket(106, "an order taken before this run"), authority=ephemeral_signer(proof)
    )

    assert evicted.trusted is True, "the program answered Ok and charged a fee"
    assert evicted.read_back is False and evicted.queues_disagree
    assert evicted.delivery.silently_did_nothing
    assert evicted.delivery.receipt_found is False
    assert fork.landed == [106], "the program was asked, and it said nothing was wrong"


def test_a_resident_order_flips_and_the_two_queues_agree() -> None:
    """The control. Without it the reading queue is just pessimism rather than evidence."""
    fork = wired(full_vec())
    proof = offline_proof(fork.rpc_url)
    kitchen = Kitchen(proof=proof, store=STORE, rpc_call=fork)

    delivered = kitchen.confirm(Ticket(126, "Water"), authority=ephemeral_signer(proof))

    assert delivered.trusted and delivered.read_back is True
    assert not delivered.queues_disagree
    assert delivered.delivery.was_delivered_before is False
    assert delivered.delivery.was_delivered_after is True


def test_a_second_delivery_needs_the_showcase_to_defer_the_store_reset() -> None:
    """WHY THE SCRIPT WITHHOLDS ONE CALL, as a test rather than as a paragraph.

    Every rehearsal resets what it touched, and the store account is one of those. Two acts
    reading the same storefront therefore cannot both see it unless the reset is deferred —
    here the fake drops the account outright, and on a real fork it reverts the
    transaction's own writes, which is the same problem wearing a subtler face.
    """
    fork = wired(full_vec())
    proof = offline_proof(fork.rpc_url)
    kitchen = Kitchen(proof=proof, store=STORE, rpc_call=fork)

    kitchen.confirm(Ticket(126, "Water"), authority=ephemeral_signer(proof))

    with pytest.raises(StoreNotFound):
        kitchen.confirm(Ticket(125, "Water"), authority=ephemeral_signer(proof))


def test_the_two_columns_come_from_one_transaction_each() -> None:
    """The comparison is not two runs — the same landed signature answers both columns."""
    fork = wired(full_vec())
    proof = offline_proof(fork.rpc_url)
    kitchen = Kitchen(
        proof=proof,
        store=STORE,
        rpc_call=deferring_store_reset(receipts_pda(STORE), fork),
    )

    confirmations = [
        kitchen.confirm(ticket, authority=ephemeral_signer(proof))
        for ticket in kitchen.pick((Ticket(106, "gone"), Ticket(126, "Water")))
    ]

    assert [c.trusted for c in confirmations] == [True, True]
    assert [c.read_back for c in confirmations] == [False, True]
    assert [c.queues_disagree for c in confirmations] == [True, False]
    assert all(c.delivery.signature for c in confirmations)
