"""Two agents, one storefront, and the moment orders start vanishing.

    uv run python -m scripts.two_agent_showcase            # starts its own fork
    uv run python -m scripts.two_agent_showcase --rpc-url http://127.0.0.1:8941

A WAITER reads the store, refuses an ambiguous request by naming the candidates, and buys.
A KITCHEN marks the order delivered and then CHECKS THE ROW CAME BACK CHANGED. Both roles
live in :mod:`gecko.sandbox.agents`; this file is the driver — it starts a fork, sequences
the three acts, and renders the two columns that are the entire point:

    what a delivery queue that TRUSTS THE TRANSACTION believes
    what a delivery queue that READS THE ROW BACK knows

One of them is losing orders, and the program never says so. The receipts vec is capped at
20 and ``make_purchase`` evicts the oldest with no error, no event and no log; a
``mark_as_delivered`` for an evicted id then confirms, charges a fee, and changes nothing.

THE CLEANUP IS DEFERRED ON PURPOSE, AND THAT IS THE ONE THING THIS SCRIPT DOES TO THE
LIBRARY. Both rehearsals end by calling ``surfnet_resetAccount`` on every address they
touched, and on a fork that reverts the TRANSACTION's writes too — measured, and stated in
:mod:`gecko.sandbox.rehearse`. That is right for one self-contained rehearsal and wrong for
a showcase whose second act must read what the first act left: reset in between and the
evicted receipt comes straight back, so the kitchen would mark a row that is there. So the
injected transport below intercepts exactly one call — ``surfnet_resetAccount`` on the
STORE account — and the script performs that reset itself at the end, printing both sides
of it. Nothing else is intercepted, no signing path is touched, and every other address the
rehearsals touch is still reset by the rehearsals themselves.

Nothing here reaches mainnet. The endpoint must prove it is a surfnet before a key can
exist (:func:`gecko.sandbox.surfnet.prove_surfnet`), both agents' keys are ephemeral, and
the store name comes from ``~/.gecko/catalog-ci.json`` so no storefront is committed here.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Sequence

from gecko.pda_testkit import SurfpoolError, SurfpoolFork, surfpool_status
from gecko.rpc import RpcCall, default_rpc_call
from gecko.sandbox.agents import (
    RECEIPTS_CAP,
    ChainQueue,
    Confirmation,
    Kitchen,
    Order,
    OrderRefused,
    Ticket,
    Waiter,
    read_chain_queue,
)
from gecko.sandbox.cheatcodes import reset_account
from gecko.sandbox.deliver import Delivery
from gecko.sandbox.rehearse import Rehearsal
from gecko.sandbox.surfnet import SurfnetProof, ephemeral_signer, prove_surfnet
from gecko.sandbox.try_purchase import _local_fork_refusal, _pin_localhost
from gecko.store_accounts import receipts_pda
from gecko.store_directory import LET_ME_BUY_PROGRAM_ID
from scripts.catalog_ci import CONFIG_PATH, ConfigError

#: A request this store genuinely cannot answer. The default is checked against the real
#: menu at runtime: if some product happens to match it exactly, the run says so rather
#: than pretending to have refused something.
AMBIGUOUS = "something to drink"

#: MEASURED, and the reason every compute number here is labelled with its chain: the same
#: purchase simulated 42,494 CU on a surfpool fork and 36,399 CU on mainnet, same minute.
FORK_VS_MAINNET = "42,494 CU on a fork vs 36,399 on mainnet, same purchase, same minute"


def store_name() -> str:
    """The storefront to run against — read from the local config, failing closed.

    Only the STORE is read. The buyer and the authority in that file are not used: both
    agents here sign with ephemeral keys that did not exist when the process started.
    """
    stored: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
    name = os.environ.get("GECKO_CI_STORE") or stored.get("store") or ""
    if not name:
        raise ConfigError(
            f'no store — write {CONFIG_PATH} with a "store" key or set GECKO_CI_STORE. '
            "The showcase reads a real storefront's own account and will not invent one."
        )
    return str(name)


def deferring_store_reset(
    store_account: str, transport: RpcCall | None = None
) -> RpcCall:
    """The transport, with ONE call withheld: the reset of the store account.

    See the module docstring. The rehearsals' own ``reset_account`` still runs and still
    reports the account's lamports either side, which for a withheld reset are simply
    equal — no claim is fabricated, and the script prints the deferral in its own output
    and performs the reset before it exits.

    ``transport`` is what everything else goes to, injectable so the wrapper itself is
    falsifiable offline against a fake fork rather than only against a validator.
    """
    underlying = transport or default_rpc_call

    def call(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "surfnet_resetAccount" and params and params[0] == store_account:
            return {"jsonrpc": "2.0", "id": 1, "result": {"value": None}}
        return underlying(rpc_url, method, params)

    return call


# ---------------------------------------------------------------------------
# which order is going to vanish
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vanishing:
    """The receipt id the kitchen will mark, and how honest a demonstration it is."""

    receipt_id: int
    why: str
    #: True only when the id is absent because the CAP evicted it. Anything else still
    #: produces the same silence from the program, for a different reason — and a demo
    #: that let those read the same would be the dishonesty it exists to expose.
    by_the_cap: bool


def choose_vanishing(queue: ChainQueue) -> Vanishing:
    """Pick the id whose row will not be there, and say plainly which case this is."""
    if queue.at_cap and queue.oldest is not None:
        return Vanishing(
            receipt_id=queue.oldest.receipt_id,
            why=(
                f"the vec is AT the cap of {RECEIPTS_CAP}, so the next make_purchase "
                "runs receipts.remove(0) and drops this row"
            ),
            by_the_cap=True,
        )
    if queue.evicted and queue.oldest is not None and queue.oldest.receipt_id > 1:
        return Vanishing(
            receipt_id=queue.oldest.receipt_id - 1,
            why=(
                f"the vec holds {len(queue.rows)} of {RECEIPTS_CAP} and the store has "
                f"recorded {queue.total_purchases} purchases, so this id was evicted by "
                "the cap BEFORE this run — the purchase below will not evict anything"
            ),
            by_the_cap=False,
        )
    return Vanishing(
        receipt_id=queue.total_purchases + 1,
        why=(
            f"the vec holds {len(queue.rows)} of {RECEIPTS_CAP} and nothing has ever been "
            "evicted from this store, so no evicted id exists. This id was never issued: "
            "the program is equally silent about it, for a DIFFERENT reason"
        ),
        by_the_cap=False,
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def rule(title: str) -> str:
    return f"\n{title}\n{'─' * max(len(title), 78)}"


def number(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def short(signature: str | None) -> str:
    return "—" if not signature else f"{signature[:8]}…"


def render_queue(queue: ChainQueue, *, when: str) -> str:
    ids = [row.receipt_id for row in queue.rows]
    span = f"{ids[0]}…{ids[-1]}" if ids else "empty"
    return (
        f"  {when:<22} resident {len(queue.rows):>2}/{RECEIPTS_CAP} [{span}]   "
        f"total_purchases {queue.total_purchases}   gone from chain {queue.evicted}   "
        f"{queue.account_bytes:,} bytes"
    )


def render_refusal(refused: OrderRefused) -> str:
    lines = [
        f'  the customer said : "{refused.request}"',
        "  the waiter said   : NO — and named what the choice is between",
        f"    candidates      : {', '.join(refused.candidates) or 'nothing listed'}",
        f"    close matches   : {', '.join(refused.close_matches) or 'none — this is not a typo, it is a category'}",
    ]
    lines += [f"    {line}" for line in _wrap(refused.reason, 74)]
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width) or [""]


def render_purchase(order: Order, rehearsal: Rehearsal) -> str:
    moved_buyer = rehearsal.buyer_token.moved if rehearsal.buyer_token else None
    moved_store = rehearsal.store_token.moved if rehearsal.store_token else None
    receipt = rehearsal.receipt
    lines = [
        f"  bought            : {order.product} at {order.price_ui} "
        f"({order.price_raw} raw) — the store's own declared price",
        f"  landed            : {short(rehearsal.signature)}   "
        f"simulated {number(rehearsal.simulated_units)} CU / charged "
        f"{number(rehearsal.units_consumed)} CU (fork)   fee "
        f"{number(rehearsal.fee_lamports)} lamports",
        f"  the ledger says   : buyer {moved_buyer!r}, store {moved_store!r} — "
        f"balanced: {rehearsal.balanced}",
    ]
    if receipt is not None:
        lines.append(
            f"  the program wrote : receipt #{receipt.receipt_id} "
            f"({receipt.product_name}, {receipt.price_raw} raw, table "
            f"{receipt.table_number}, delivered={receipt.delivered})   "
            f"total_purchases {receipt.total_purchases_before} → "
            f"{receipt.total_purchases_after}"
        )
    for problem in rehearsal.discrepancies:
        lines += [f"  ! {line}" for line in _wrap(problem, 74)]
    for refusal in rehearsal.refusals:
        lines.append(f"  ! refused at {refusal.step}: {refusal.reason}")
    return "\n".join(lines)


def render_delivery(confirmation: Confirmation) -> str:
    delivery: Delivery = confirmation.delivery
    taken = delivery.took_the_store
    lines = [
        f"  ticket #{confirmation.ticket.receipt_id} ({confirmation.ticket.product})",
        f"    took the store  : {'yes — ' + taken.authority_before[:8] + '… → ' + taken.authority_after[:8] + '…, ' + str(taken.collateral_changes) + ' collateral byte(s)' if taken else 'no'}"
        + "   [cheatcode, fork only — no such move exists on any real chain]",
        f"    landed          : {short(delivery.signature)}   "
        f"{number(delivery.units_consumed)} CU (fork)   fee "
        f"{number(delivery.fee_lamports)} lamports   scanning "
        f"{number(delivery.resident_receipts)} rows / "
        f"{number(delivery.resident_bytes)} bytes",
        f"    the row says    : found={delivery.receipt_found}   was_delivered "
        f"{delivery.was_delivered_before!r} → {delivery.was_delivered_after!r}   "
        f"silently_did_nothing={delivery.silently_did_nothing}",
    ]
    for problem in delivery.discrepancies:
        lines += [f"    ! {line}" for line in _wrap(problem, 70)]
    return "\n".join(lines)


def render_columns(confirmations: Sequence[Confirmation]) -> str:
    """The two columns. Same transactions, same instant — two different conclusions."""
    rows: list[tuple[str, str, str]] = []
    for confirmation in confirmations:
        delivery = confirmation.delivery
        trusts = (
            f"DELIVERED — {short(delivery.signature)} confirmed, err null, "
            f"{number(delivery.units_consumed)} CU"
            if confirmation.trusted
            else "nothing landed"
        )
        if confirmation.read_back is True:
            reads = "delivered — the row went false → true"
        elif not delivery.receipt_found:
            reads = (
                f"STILL OPEN — no row #{confirmation.ticket.receipt_id} in the vec "
                f"({number(delivery.resident_receipts)} scanned)"
            )
        else:
            reads = (
                f"STILL OPEN — the row is there and was_delivered is "
                f"{delivery.was_delivered_after!r}"
            )
        rows.append((f"#{confirmation.ticket.receipt_id}", trusts, reads))
    # Width from the content, so a longer signature or a wider CU number cannot run the
    # two columns into each other — which would hide the very comparison this table is.
    left = max([len(trusts) for _, trusts, _ in rows] + [36]) + 3
    lines = [
        f"  {'order':<8}{'A QUEUE THAT TRUSTS THE TRANSACTION':<{left}}"
        "A QUEUE THAT READS THE ROW BACK",
        "  " + "─" * (left + 40),
    ]
    lines += [f"  {order:<8}{trusts:<{left}}{reads}" for order, trusts, reads in rows]
    lost = sum(1 for c in confirmations if c.queues_disagree)
    charged = sorted(
        {
            c.delivery.units_consumed
            for c in confirmations
            if c.delivery.units_consumed is not None
        }
    )
    lines += [
        "",
        f"  The two columns disagree on {lost} of {len(confirmations)} order(s). Every "
        "transaction above succeeded.",
        "  A venue reading the left column loses those orders and is never told.",
    ]
    if len(charged) > 1:
        lines.append(
            "  (The deliveries charged "
            + " and ".join(number(value) for value in charged)
            + " CU — this instruction SCANS the vec, so its"
        )
        lines.append(
            "  compute tracks resident bytes and neither figure is a budget for the other.)"
        )
    return "\n".join(lines)


def render_honesty(proof: SurfnetProof, *, store: str, reset_line: str) -> str:
    return "\n".join(
        [
            f"  CHAIN     Every number above came from {proof.rpc_url} — a LOCAL",
            f"            surfpool mainnet fork, proved at slot {proof.slot} by a method",
            "            mainnet answers -32601 to. A FORK IS NOT A COMPUTE ORACLE:",
            f"            measured {FORK_VS_MAINNET}.",
            "            Read every CU here as the fork's, not the chain's.",
            "  MAINNET   Nothing here WROTE to it. No mainnet transaction was signed, sent",
            "            or simulated. The fork does READ mainnet accounts on demand — that",
            "            is how a real storefront's own bytes got here — and every write",
            "            above landed in the fork and nowhere else.",
            "  KEYS      Both agents' keypairs were generated AFTER the endpoint proved",
            "            itself, held in memory, never written to disk, never logged, and",
            "            are gone when this process exits.",
            f"  SUBJECT   let_me_buy ({LET_ME_BUY_PROGRAM_ID}) is a",
            "            THIRD-PARTY program. The eviction, the silent Ok and the missing",
            "            authority check are its behaviour, read the same way its own",
            "            publisher can read it — not a defect in anyone's catalogue. What an",
            "            IDL cannot express is the whole point: no field in it says 20.",
            f"  STATE     The fork was written to (funding, the authority take on {store}'s",
            "            account) and restored before exit:",
            f"            {reset_line}",
        ]
    )


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def showcase(
    proof: SurfnetProof, *, store: str, request: str, exact: str | None
) -> int:
    """The three acts, against a proven fork. Returns a process exit code."""
    receipts = receipts_pda(store)
    transport = deferring_store_reset(receipts)
    waiter = Waiter(proof=proof, store=store, rpc_call=transport)
    kitchen = Kitchen(proof=proof, store=store, rpc_call=transport)

    menu = waiter.menu()
    opening = read_chain_queue(store, rpc_url=proof.rpc_url, rpc_call=transport)
    print(rule(f"THE STOREFRONT — {store} @ {receipts}"))
    print(f"  authority         : {menu.authority}")
    for entry in menu.products:
        print(f"  on the menu       : {entry.name} at {entry.price_ui} ({entry.mint})")
    print(render_queue(opening, when="before anything:"))
    vanishing = choose_vanishing(opening)
    if opening.at_cap:
        print(
            f"  AT THE CAP        : {vanishing.why}. This run can therefore show the "
            "finding."
        )
    else:
        print(
            f"  NOT AT THE CAP    : the finding needs a full vec and this store holds "
            f"{len(opening.rows)}/{RECEIPTS_CAP}. {vanishing.why}."
        )

    print(rule("ACT 1 — the waiter refuses to guess"))
    refused = waiter.take_order(request, menu=menu)
    if isinstance(refused, Order):
        print(
            f'  "{request}" resolved to {refused.product} — this store DOES list it, so '
            "nothing was refused. Pass --request with something the menu cannot answer."
        )
    else:
        print(render_refusal(refused))

    print(rule("ACT 2 — the waiter buys exactly what was named"))
    wanted = exact or menu.products[0].name
    order = waiter.take_order(wanted, menu=menu)
    if isinstance(order, OrderRefused):
        print(render_refusal(order))
        return 1
    rehearsal = waiter.buy(order, buyer=ephemeral_signer(proof), table_number=7)
    print(render_purchase(order, rehearsal))
    if not rehearsal.landed:
        print("  the purchase did not land — nothing below would be evidence")
        return 1
    after_buy = read_chain_queue(store, rpc_url=proof.rpc_url, rpc_call=transport)
    print(render_queue(after_buy, when="after the purchase:"))
    bought_id = rehearsal.receipt.receipt_id if rehearsal.receipt else None
    still_there = any(row.receipt_id == vanishing.receipt_id for row in after_buy.rows)
    print(
        f"  receipt #{vanishing.receipt_id} is "
        + ("STILL RESIDENT" if still_there else "GONE")
        + f" and the store recorded #{bought_id}. No error, no event, no log: "
        "the vec length did not change and the counter did."
    )

    print(rule("ACT 3 — the kitchen marks them delivered, and reads the row back"))
    tickets = [Ticket(vanishing.receipt_id, "an order taken before this run")]
    if bought_id is not None:
        tickets.append(Ticket(bought_id, order.product))
    print(
        f"  the venue's own queue holds {[t.receipt_id for t in tickets]} — its memory of "
        "orders it took,\n  which is NOT the receipts vec and does not shrink when the vec "
        "does."
    )
    confirmations = [
        kitchen.confirm(ticket, authority=ephemeral_signer(proof))
        for ticket in kitchen.pick(tickets)
    ]
    for confirmation in confirmations:
        print(render_delivery(confirmation))

    print(rule("WHAT EACH KIND OF DELIVERY QUEUE BELIEVES"))
    print(render_columns(confirmations))
    return 0 if any(c.queues_disagree for c in confirmations) else 1


def run(proof: SurfnetProof, *, store: str, request: str, exact: str | None) -> int:
    """:func:`showcase`, with the deferred store reset guaranteed and reported."""
    receipts = receipts_pda(store)
    try:
        return showcase(proof, store=store, request=request, exact=exact)
    finally:
        outcome = reset_account(proof, receipts)
        restored = read_chain_queue(store, rpc_url=proof.rpc_url)
        print(rule("NOTHING HERE IS LEFT BEHIND"))
        line = (
            f"the store account was reset LAST (deferred so act 3 could read what act 2 "
            f"left): {outcome.lamports_before} → {outcome.lamports_after} lamports, and it "
            f"now reads {len(restored.rows)} resident rows / total_purchases "
            f"{restored.total_purchases} — the fork's view of mainnet, unwritten."
        )
        print("  " + "\n  ".join(_wrap(line, 76)))
        print(rule("WHAT THIS RUN DOES NOT CLAIM"))
        print(
            render_honesty(
                proof,
                store=store,
                reset_line=(
                    f"the store account now reads {len(restored.rows)} rows / "
                    f"total_purchases {restored.total_purchases} — mainnet's own state."
                ),
            )
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("GECKO_FORK_RPC"),
        help="attach to a surfpool fork already running on THIS machine",
    )
    parser.add_argument("--port", type=int, default=8941)
    parser.add_argument(
        "--mainnet-rpc",
        default=os.environ.get(
            "GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com"
        ),
        help="what the fork forks. Never called by this script directly.",
    )
    parser.add_argument("--store", default=None)
    parser.add_argument("--request", default=AMBIGUOUS)
    parser.add_argument(
        "--product",
        default=None,
        help="the exact listed name to buy (default: the first)",
    )
    args = parser.parse_args(argv)
    store = args.store or store_name()

    if args.rpc_url:
        rpc_url = _pin_localhost(str(args.rpc_url).strip())
        refusal = _local_fork_refusal(rpc_url)
        if refusal:
            raise SystemExit(f"the showcase refused: {refusal}")
        return run(
            prove_surfnet(rpc_url),
            store=store,
            request=args.request,
            exact=args.product,
        )

    status = surfpool_status()
    if not status.available:
        raise SystemExit(f"the showcase did not run: {status.detail}")
    try:
        with SurfpoolFork(
            args.mainnet_rpc, port=args.port, ready_timeout=180
        ) as running:
            return run(
                prove_surfnet(running.rpc_url),
                store=store,
                request=args.request,
                exact=args.product,
            )
    except SurfpoolError as exc:
        raise SystemExit(f"the showcase did not run — no fork: {exc}") from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        raise SystemExit(f"the showcase refused: {exc}") from exc
