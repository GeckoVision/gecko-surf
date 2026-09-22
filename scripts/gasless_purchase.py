"""One relay-paid purchase, end to end, with every rail on: the fork rehearsal runner.

    uv run python scripts/gasless_purchase.py \\
        --network fork --rpc-url http://127.0.0.1:8899 \\
        --buyer-keypair ~/.gecko/wallets/usdg-nosol-buyer.json \\
        --store geckocoffee --product Espresso [--table 1] [--max-spend-usdc 0.25] \\
        [--broadcast]

    KORA_RPC_URL=http://127.0.0.1:8080  KORA_API_KEY=...   in the environment

What it does, in order, and each step is the package's, not this file's:

  1. ``prepare_purchase`` (the MCP tool) with ``fee_payer`` = the relay's own account:
     resolved accounts, a passing receipt, UNSIGNED bytes bound at ``exact``.
  2. Without ``--broadcast`` it stops here and prints what it would have sent. The relay
     is never asked for a signature in a dry run, because a Kora signature is a real
     policy decision on the relay's side and a real simulation on its node.
  3. With ``--broadcast``: :func:`gecko.autonomous_purchase.settle_sponsored`. The relay
     signs first (and appends its Lighthouse assertion); the bytes are re-simulated and
     re-verified; the buyer signs in the ``authority`` role behind the spend gate; the two
     signatures are merged; ONE transaction is sent and confirmed.
  4. Judge by what MOVED: the buyer's SOL before and after must be equal. That line is
     the gasless claim; everything else is the purchase working.

The buyer's key is read from a file by :class:`AuthorityKeypairBackend`, which signs its
own slot and nothing else, outside ``gecko/``. The relay's key is never here at all.

Per CLAUDE.md the mainnet broadcast is founder-run: ``--network mainnet --broadcast``
spends real money on both sides and is the founder's decision to type.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.autonomous_purchase import (  # noqa: E402
    PurchaseSettled,
    default_spend_policy,
    settle_route,
    settle_sponsored,
    swap_spend_policy,
)
from gecko.networks import coerce_network  # noqa: E402
from gecko.prepare_purchase import prepare_purchase_result  # noqa: E402
from gecko.rpc import default_rpc_call, validate_rpc_url  # noqa: E402
from gecko.sandbox import ephemeral_signer, prove_surfnet  # noqa: E402
from gecko.sandbox.cheatcodes import fund_sol  # noqa: E402
from gecko.sandbox.rehearse import rehearse_purchase  # noqa: E402
from operator_policy import USDG_ACCEPTED  # noqa: E402
from gecko.signer import (  # noqa: E402
    AUTHORITY_ROLE,
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SigningAttestation,
    TransactionSigner,
)
from gecko.spend_policy import InMemorySpendLedger, SpendPolicyGate  # noqa: E402
from gecko.trace import Trace  # noqa: E402
from scripts.kora_relay import KoraRelay, KoraRelayError  # noqa: E402
from scripts.paybox_backend import (  # noqa: E402
    PayboxAuthorityBackend,
    PayboxBackendError,
)


class AuthorityKeypairBackend:
    """The buyer's key, OUTSIDE ``gecko/``. Signs ITS slot; leaves the relay's alone.

    Different from ``scripts/autonomous_purchase.py``'s ``LocalKeypairBackend``, which
    populates slot 0 because it IS the fee payer. Under a relay the buyer is slot 1, and
    ``partial_sign`` puts the signature where the message says this key belongs.
    """

    def __init__(self, path: Path) -> None:
        from solders.keypair import Keypair

        self._keypair = Keypair.from_bytes(bytes(json.loads(path.read_text())))

    @property
    def pubkey(self) -> str:
        return str(self._keypair.pubkey())

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        from solders.transaction import Transaction

        if attestation.signing_as != AUTHORITY_ROLE:
            raise RuntimeError(
                "this backend signs as the authority, never as fee payer"
            )
        transaction = Transaction.from_bytes(unsigned_transaction)
        transaction.partial_sign([self._keypair], transaction.message.recent_blockhash)
        return bytes(transaction)


def _balance(rpc_url: str, account: str) -> int:
    # `confirmed`, the commitment the landing is confirmed at: the default (finalized)
    # read "relay SOL after == before" on 2026-09-18 while the chain already showed the fee.
    reply = default_rpc_call(
        rpc_url, "getBalance", [account, {"commitment": "confirmed"}]
    )
    value = (reply.get("result") or {}).get("value")
    return int(value) if isinstance(value, int) else 0


def _emit_trace(args: argparse.Namespace, trace: Trace) -> None:
    """Write the trace, and the graph drawn from it, when asked. Never on the hot path."""
    if args.trace is None:
        return
    trace.write(args.trace)
    print(f"  trace      {args.trace}  ({len(trace.rows)} steps)")
    if args.graph is not None:
        from scripts.trace_to_graph import render, spec_from_trace

        spec_path = args.graph.with_suffix(".sequence.json")
        spec_path.write_text(json.dumps(spec_from_trace(trace), indent=2) + "\n")
        receipt = render(spec_path, args.graph)
        if receipt.get("rendered"):
            print(f"  graph      {args.graph}")
        else:
            print(
                f"  graph      NOT rendered: "
                f"{receipt.get('reason') or receipt.get('stderr')}"
            )


USDG_MINT = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _rehearse_route_on_fork(
    args: argparse.Namespace, proof: Any, buyer: Any, relay: KoraRelay
) -> int:
    """Convert on Orca, then buy; the relay pays both fees; the buyer never holds SOL."""
    from gecko.orquestra_build import orquestra_seams
    from gecko.providers.whirlpool import WHIRLPOOL_PROGRAM, plan_swap
    from gecko.sandbox.rehearse_route import RouteLeg, rehearse_gasless_route

    idl_fetch, build_call = orquestra_seams()
    plan = plan_swap(
        {
            "input_mint": USDG_MINT,
            "output_mint": USDC_MINT,
            "user": buyer.pubkey,
            "amount_in": args.convert_amount,
        },
        rpc_url=args.rpc_url,
        idl_fetch=idl_fetch,
    )
    if plan.get("refused"):
        print(f"REFUSED at plan_swap [{plan.get('code')}]: {plan.get('reason')}")
        return 1
    quote = plan.get("quote") or {}
    print(
        f"  convert    {args.convert_amount} USDG -> USDC on {str(plan.get('pool'))[:8]}…  "
        f"min out {quote.get('min_amount_out')}"
    )
    trace = Trace(lane="route", network="fork")
    route = rehearse_gasless_route(
        proof,
        buyer=buyer,
        relay=relay,
        convert=RouteLeg(
            program_id=WHIRLPOOL_PROGRAM,
            instruction="swap_v2",
            values=plan["values"],
            # swap_v2 creates no token accounts: the USDC one must exist, empty.
            fund_tokens=[(USDG_MINT, args.convert_amount, TOKEN_2022), (USDC_MINT, 0)],
            idl_fetch=idl_fetch,
            build_call=build_call,
        ),
        store=args.store,
        product=args.product,
        table_number=args.table,
        trace=trace,
    )
    _emit_trace(args, trace)
    c = route.convert
    print(
        f"  leg 1      {'LANDED' if c.landed else 'NOT LANDED'} {c.signature or ''}  "
        f"CU {c.compute_units}  relay SOL {c.relay_sol.moved if c.relay_sol else None}"
    )
    for refusal in c.refusals:
        print(f"             refused at {refusal.step}: {refusal.reason}")
    for delta in c.token_deltas:
        print(f"             {delta.mint[:8]}… {delta.before} -> {delta.after}")
    p = route.purchase
    if p is not None:
        print(
            f"  leg 2      {'LANDED' if p.landed else 'NOT LANDED'} {p.signature or ''}  "
            f"CU simulated {p.simulated_units} charged {p.units_consumed}  "
            f"relay SOL {p.relay_sol.moved if p.relay_sol else None}"
        )
        for refusal in p.refusals:
            print(f"             refused at {refusal.step}: {refusal.reason}")
    print(
        f"  buyer SOL  {route.buyer_sol.before} -> {route.buyer_sol.after}   "
        f"(None = account never existed)"
    )
    print(f"  relay SOL  moved {route.relay_sol.moved} across both legs")
    for line in route.objections:
        print(f"  OBJECTION  {line}")
    if route.landed and not route.objections:
        print("GASLESS ROUTE: converted and bought; the buyer's SOL never moved.")
        return 0
    return 1


def _record_decision(
    network: Any,
    *,
    lane: str,
    programs: tuple[str, ...],
    terminal: str,
    code: str | None = None,
    signatures: tuple[str, ...] = (),
    at_stake: tuple[tuple[str, int, int], ...] = (),
    trace: Trace | None = None,
) -> None:
    """One row in the decision log per mainnet run: landed, refused by name, or abandoned."""
    from gecko.decision_log import append_decision, decision_row

    row = decision_row(
        trace=trace or Trace(lane=lane, network=str(network)),
        lane=lane,
        network=str(network),
        programs=programs,
        terminal=terminal,  # type: ignore[arg-type]
        code=code,
        signatures=signatures,
        at_stake=at_stake,
    )
    path = append_decision(row)
    print(f"  decision   {terminal}{f' [{code}]' if code else ''}  -> {path}")


def _product_price_raw(args: argparse.Namespace, network: Any) -> int | None:
    """The product's price in its mint's raw units, read from the store listing, or None."""
    from gecko.store_directory import list_stores_result

    listing = list_stores_result(
        {"store": args.store, "network": network, "rpc_url": args.rpc_url}
    )
    for store in listing.get("stores") or []:
        for product in store.get("products") or []:
            if product.get("name") == args.product and product.get("mint") == USDC_MINT:
                price = product.get("price_raw")
                return int(price) if isinstance(price, int) else None
    return None


def _token_balance(rpc_url: str, owner: str, mint: str) -> int:
    """Raw balance of ``mint`` held by ``owner`` across its token accounts, at `confirmed`."""
    reply = default_rpc_call(
        rpc_url,
        "getTokenAccountsByOwner",
        [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    total = 0
    for entry in (reply.get("result") or {}).get("value") or []:
        info = entry["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += int(info["amount"])
    return total


def _settle_route_on_mainnet(
    args: argparse.Namespace,
    network: Any,
    relay: KoraRelay,
    buyer: Any,
    buyer_sol_before: int,
    relay_sol_before: int,
) -> int:
    """USDG -> USDC on Orca, then the purchase; the relay pays both fees; the buyer's
    wallet signs both legs and never holds SOL. The mainnet sibling of the fork route.
    Dry run prepares and simulates both legs; --broadcast settles them in order."""
    from gecko.landing import latest_blockhash
    from gecko.orquestra_build import orquestra_seams
    from gecko.prepare_instruction import prepare_instruction_result
    from gecko.providers.whirlpool import WHIRLPOOL_PROGRAM, plan_swap
    from gecko.simulate import BuiltTx, simulate
    from gecko.token_program import read_mint_extensions

    held = _token_balance(args.rpc_url, buyer.pubkey, USDG_MINT)
    print(f"  buyer USDG         {held}   (converting {args.convert_amount})")
    if held < args.convert_amount:
        print(
            f"STOP: the buyer holds {held} USDG and the convert leg needs "
            f"{args.convert_amount}; nothing here guesses a smaller amount for you"
        )
        return 2

    # The mint's extension set is READ now and checked against the acceptance above by
    # the simulation; the gate then checks the acceptance the simulation applied against
    # the policy. Printing both here is so a refusal names what moved.
    evidence = read_mint_extensions([USDG_MINT], rpc_url=args.rpc_url)
    read = evidence.get(USDG_MINT)
    if read is None:
        print(
            "STOP: the USDG mint's extension set could not be read; unread is refused"
        )
        return 2
    print(
        f"  USDG extensions    {', '.join(read.names)}  hook program "
        f"{read.transfer_hook_program}"
    )
    if not USDG_ACCEPTED.matches(read):
        print(
            "STOP: the USDG mint no longer reads as the accepted state; a human looks "
            "before the pin is changed"
        )
        _record_decision(
            network,
            lane="route",
            programs=("whirlpool", "let_me_buy"),
            terminal="abandoned",
            code="mint-extensions-changed",
            at_stake=((USDG_MINT, args.convert_amount, 6),),
        )
        return 2
    print("  acceptance         matches the mint as read")
    accepted = {USDG_MINT: USDG_ACCEPTED}

    idl_fetch, build_call = orquestra_seams()
    plan = plan_swap(
        {
            "input_mint": USDG_MINT,
            "output_mint": USDC_MINT,
            "user": buyer.pubkey,
            "amount_in": args.convert_amount,
        },
        rpc_url=args.rpc_url,
        idl_fetch=idl_fetch,
    )
    if plan.get("refused"):
        print(f"REFUSED at plan_swap [{plan.get('code')}]: {plan.get('reason')}")
        return 1
    quote = plan.get("quote") or {}
    print(
        f"  convert    {args.convert_amount} USDG -> USDC on {str(plan.get('pool'))[:8]}…  "
        f"min out {quote.get('min_amount_out')}"
    )
    # Can the purchase be paid AFTER the convert lands? Measured 2026-09-19: leg 1 landed
    # and leg 2 refused at prepare because the USDC held plus the swap's output was under
    # the price; the swap was real money spent for a purchase that could not follow. The
    # quote's minimum out is the floor the swap guarantees, so held + floor >= price is
    # the only reading that never signs leg 1 for nothing.
    price = _product_price_raw(args, network)
    held_usdc = _token_balance(args.rpc_url, buyer.pubkey, USDC_MINT)
    floor = int(quote.get("min_amount_out") or 0)
    if price is None:
        print("STOP: the product's price could not be read; nothing here guesses it")
        return 2
    if held_usdc + floor < price:
        print(
            f"STOP: after converting, the buyer would hold at most {held_usdc} + "
            f"{floor} = {held_usdc + floor} raw USDC and the product costs {price}; "
            f"raise --convert-amount or fund the wallet, then run again"
        )
        _record_decision(
            network,
            lane="route",
            programs=("whirlpool", "let_me_buy"),
            terminal="abandoned",
            code="route-unaffordable",
            at_stake=((USDG_MINT, args.convert_amount, 6), (USDC_MINT, price, 6)),
        )
        return 2
    print(
        f"  purchase   {price} raw USDC; held {held_usdc} + swap floor {floor} covers it"
    )
    prepared = prepare_instruction_result(
        {
            "program_id": WHIRLPOOL_PROGRAM,
            "instruction": "swap_v2",
            "payer": buyer.pubkey,
            "fee_payer": relay.pubkey,
            "values": dict(plan["values"]),
        },
        idl_fetch=idl_fetch,
        build_call=build_call,
        rpc_url=args.rpc_url,
    )
    if prepared.get("refused"):
        print(
            f"REFUSED at prepare (convert) [{prepared.get('code')}]: {prepared.get('reason')}"
        )
        return 1
    convert_unsigned = str(prepared["transaction_base64"])
    receipt = simulate(
        {},
        rpc_url=args.rpc_url,
        build_call=lambda _plan: BuiltTx(tx=convert_unsigned, encoding="base64"),
        replace_blockhash=False,
        network_label=f"simulated against {network} (convert leg, unsigned)",
        network=network,
        track=[buyer.pubkey],
        mint_extensions=evidence,
        accepted_mints=accepted,
    )
    print(
        f"\n  CONVERT  {receipt.status.upper()}  {receipt.units_consumed or 0:,} CU  "
        f"signers {(prepared.get('gasless') or {}).get('signatures_required')}"
    )
    if receipt.status != "pass":
        print(f"REFUSED: the convert leg does not simulate ({receipt.revert_class})")
        return 1
    leg = receipt.token_delta
    if leg is None or leg.status != "measured":
        why = (
            "not tracked"
            if leg is None
            else "; ".join(f"[{r.reason}] {r.detail}" for r in leg.refusals)
        )
        print(f"REFUSED: the convert leg's token movement is not measurable: {why}")
        print("         the spend gate would refuse this leg; nothing was signed")
        return 1
    sold = [o for o in leg.outflows() if o.owner == buyer.pubkey]
    print(
        "  token leg  measured: "
        + ", ".join(f"{o.ui} of {o.mint[:8]}… leaves the buyer" for o in sold)
        + f"  (acceptance applied: {[a.mint[:8] + '…' for a in leg.acceptances]})"
    )

    def prepare_purchase() -> dict[str, Any]:
        return prepare_purchase_result(
            {
                "store": args.store,
                "product": args.product,
                "buyer": buyer.pubkey,
                "fee_payer": relay.pubkey,
                "table": args.table,
                "network": network,
                "rpc_url": args.rpc_url,
            }
        )

    # The purchase is prepared for real only AFTER the convert leg lands (settle_route
    # does that); this dry prepare names the accounts the shop's gate may write, and
    # says whether the purchase already simulates on the wallet as it stands.
    dry = prepare_purchase()
    if dry.get("refused"):
        print(
            f"  PURCHASE (dry) refused now [{dry.get('code')}]: {str(dry.get('reason'))[:120]}"
        )
        print(
            "             (expected when the wallet lacks the priced mint until the convert leg lands)"
        )
    else:
        print(f"  PURCHASE (dry) {dry['status'].upper()}  {dry['units_consumed']:,} CU")

    if not args.broadcast:
        print("\nDRY RUN: both legs prepared; the relay was not asked to sign.")
        print("Re-run with --broadcast to convert, then buy, relay-paid.")
        return 0

    convert_gate = SpendPolicyGate(
        policy=swap_spend_policy(
            allowed_destinations=frozenset(
                a for a in dict(prepared["accounts"]).values() if a != buyer.pubkey
            ),
            input_mint=USDG_MINT,
            input_decimals=6,
            input_per_transaction_raw=args.convert_amount,
            accepted_mints=(USDG_ACCEPTED,),
        ),
        ledger=InMemorySpendLedger(),
    )
    plan_accounts = dry.get("accounts") or {}
    entries = (
        plan_accounts.values() if isinstance(plan_accounts, dict) else plan_accounts
    )
    purchase_gate = SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=frozenset(
                e["address"]
                for e in entries
                if e.get("writable") and e["address"] != buyer.pubkey
            ),
            sponsored=True,
            usdc_per_transaction_raw=int(round(args.max_spend_usdc * 1_000_000)),
        ),
        ledger=InMemorySpendLedger(),
    )
    profile_name = (
        EXTERNAL_SIGNER_PROFILE_NAME
        if args.signer == "paybox"
        else DEVELOPER_KEYPAIR_FILE_PROFILE_NAME
    )

    def signer_with(gate: SpendPolicyGate) -> TransactionSigner:
        return TransactionSigner(
            backend=buyer,
            profile=SignerProfile(
                name=profile_name,
                network=network,
                authorized=True,
                signing_as=AUTHORITY_ROLE,
            ),
            spend_gate=gate,
        )

    _, last_valid = latest_blockhash(args.rpc_url, default_rpc_call)
    trace = Trace(lane="route", network=str(network))
    route = settle_route(
        convert_unsigned,
        prepare_purchase=prepare_purchase,
        network=network,
        rpc_url=args.rpc_url,
        relay=relay,
        convert_signer=signer_with(convert_gate),
        purchase_signer=signer_with(purchase_gate),
        authority=buyer.pubkey,
        mint_extensions=evidence,
        convert_accepted_mints=accepted,
        convert_last_valid_block_height=int(last_valid),
        trace=trace,
    )
    _emit_trace(args, trace)
    for label, leg in (("leg 1", route.convert), ("leg 2", route.purchase)):
        if leg is None:
            print(f"  {label}      not attempted")
        elif isinstance(leg, PurchaseSettled):
            print(
                f"  {label}      LANDED {leg.signature}  CU predicted {leg.predicted_units} "
                f"charged {leg.consumed_units}"
            )
        else:
            print(f"  {label}      REFUSED [{leg.code}]: {leg.reason}")
    buyer_sol_after = _balance(args.rpc_url, buyer.pubkey)
    relay_sol_after = _balance(args.rpc_url, relay.pubkey)
    print(f"\n  buyer SOL after    {buyer_sol_after}   (before {buyer_sol_before})")
    print(f"  relay SOL after    {relay_sol_after}   (before {relay_sol_before})")
    for line in route.objections:
        print(f"  OBJECTION  {line}")
    legs = [leg for leg in (route.convert, route.purchase) if leg is not None]
    refused_leg = next(
        (leg for leg in legs if not isinstance(leg, PurchaseSettled)), None
    )
    _record_decision(
        network,
        lane="route",
        programs=("whirlpool", "let_me_buy"),
        terminal="landed" if route.landed else "refused",
        code=None
        if route.landed
        else (refused_leg.code if refused_leg else "not-landed"),
        signatures=tuple(
            leg.signature for leg in legs if isinstance(leg, PurchaseSettled)
        ),
        at_stake=((USDG_MINT, args.convert_amount, 6), (USDC_MINT, price, 6)),
        trace=trace,
    )
    if buyer_sol_after != buyer_sol_before:
        print("NOT GASLESS: the buyer's SOL moved.")
        return 1
    if route.landed:
        print(
            "GASLESS ROUTE: converted and bought on mainnet; the buyer's SOL never moved."
        )
        return 0
    return 1


def _rehearse_on_fork(args: argparse.Namespace, relay: KoraRelay) -> int:
    """The fork lane: `gecko.sandbox.rehearse`, judged by what moved.

    Not the spend-gate path. A fork cannot report a token leg (surfpool nulls the
    balance arrays), so the gate would refuse every purchase here for a reason that is
    about the node, not the bytes. The sandbox lane lands the transaction and reads the
    ledger afterwards, which is the only measurement a fork supports. The buyer is an
    ephemeral key that exists only because the endpoint proved it is a fork; it is funded
    with the price and NO SOL, so "gasless" is measured, not assumed.
    """
    proof = prove_surfnet(args.rpc_url)
    buyer = ephemeral_signer(proof)
    print(f"  fork proven        {proof.rpc_url}")
    print(f"  relay (fee payer)  {relay.pubkey}")
    print(f"  buyer (ephemeral)  {buyer.pubkey}   funded with tokens only")
    relay_sol = _balance(args.rpc_url, relay.pubkey)
    if relay_sol < 10_000_000:
        # The relay's key is the operator's; on a fork its balance is a cheatcode away.
        fund_sol(proof, relay.pubkey, 50_000_000)
        print("  relay funded on the fork by cheatcode (0.05 SOL)")
    if not args.broadcast:
        print(
            "\nDRY RUN: on a fork the rehearsal is the run; add --broadcast to land it."
        )
        return 0

    if args.convert_from:
        return _rehearse_route_on_fork(args, proof, buyer, relay)

    trace = Trace(lane="rehearsal", network="fork")
    result = rehearse_purchase(
        proof,
        buyer=buyer,
        store=args.store,
        product=args.product,
        table_number=args.table,
        relay=relay,
        trace=trace,
    )
    _emit_trace(args, trace)
    for refusal in result.refusals:
        print(f"  REFUSED at {refusal.step}: {refusal.reason}")
    if not result.landed:
        return 1
    print(f"\n  LANDED   {result.signature}")
    print(
        f"  CU       simulated {result.simulated_units}  charged {result.units_consumed}"
    )
    print(f"  signers  {result.signatures}  fee payer {result.fee_payer}")
    buyer_sol = result.buyer_sol
    relay_delta = result.relay_sol
    print(
        f"  buyer SOL  {buyer_sol.before if buyer_sol else None} -> "
        f"{buyer_sol.after if buyer_sol else None}   (None = account never existed)"
    )
    print(
        f"  relay SOL  moved {relay_delta.moved if relay_delta else None}   "
        f"(fee {result.fee_lamports})"
    )
    bt, st = result.buyer_token, result.store_token
    print(
        f"  buyer tok  moved {bt.moved if bt else None}   store tok moved {st.moved if st else None}"
    )
    if result.receipt is not None:
        print(
            f"  receipt    row #{result.receipt.receipt_id} "
            f"{result.receipt.product_name!r} price {result.receipt.price_raw}"
        )
    for line in result.discrepancies:
        print(f"  DISCREPANCY  {line}")
    print(f"  reset      {len(result.reset)} accounts restored")
    if result.discrepancies:
        return 1
    print("GASLESS: the buyer's SOL did not move; the relay paid; the ledger balances.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--network", choices=["fork", "mainnet"], required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--buyer-keypair", type=Path, default=None)
    parser.add_argument(
        "--signer",
        choices=["keypair", "paybox"],
        default="keypair",
        help="mainnet only: who holds the buyer's key. paybox = the SDK CLI, "
        "PAYBOX_TOKEN + PAYBOX_SIGNIN_KEY in the environment, an autonomous wallet",
    )
    parser.add_argument(
        "--paybox-credential", default=None, help="pin one PayBox wallet credential id"
    )
    parser.add_argument("--store", default="geckocoffee")
    parser.add_argument("--product", required=True)
    parser.add_argument("--table", type=int, default=1)
    parser.add_argument("--max-spend-usdc", type=float, default=1.0)
    parser.add_argument(
        "--convert-from",
        choices=["USDG"],
        default=None,
        help="fork only: start from this token instead of the product's; converts on "
        "Orca first, relay-paid, then buys. The USDG story, end to end, gasless",
    )
    parser.add_argument(
        "--convert-amount",
        type=int,
        default=110_000,
        help="base units of --convert-from to swap (default 0.11, enough for a 0.10 "
        "product at the measured USDG/USDC rate)",
    )
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument(
        "--trace",
        type=Path,
        default=None,
        help="write the run's trace (JSONL, control plane only) here",
    )
    parser.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="also render the trace as an archify sequence HTML here",
    )
    args = parser.parse_args(argv)
    network = coerce_network(args.network)

    try:
        relay = KoraRelay.from_env()
    except KoraRelayError as exc:
        print(f"STOP: relay not reachable: {exc}")
        return 2
    if network == "fork":
        return _rehearse_on_fork(args, relay)
    buyer: AuthorityKeypairBackend | PayboxAuthorityBackend
    if args.signer == "paybox":
        try:
            buyer = PayboxAuthorityBackend.open(credential=args.paybox_credential)
        except PayboxBackendError as exc:
            print(f"STOP: PayBox signer not usable: {exc}")
            return 2
        print(
            f"  paybox wallet      {buyer.wallet.name} ({buyer.wallet.approval_mode})"
        )
    else:
        if args.buyer_keypair is None:
            print("STOP: --buyer-keypair is required with --signer keypair on mainnet")
            return 2
        buyer = AuthorityKeypairBackend(args.buyer_keypair)
    print(f"  relay (fee payer)  {relay.pubkey}")
    print(f"  buyer (authority)  {buyer.pubkey}")

    buyer_sol_before = _balance(args.rpc_url, buyer.pubkey)
    relay_sol_before = _balance(args.rpc_url, relay.pubkey)
    print(f"  buyer SOL before   {buyer_sol_before}")
    print(f"  relay SOL before   {relay_sol_before}")

    if args.convert_from:
        return _settle_route_on_mainnet(
            args, network, relay, buyer, buyer_sol_before, relay_sol_before
        )

    # 1. PREPARE, with the relay as payer. Every refusal the tool makes, this makes.
    out = prepare_purchase_result(
        {
            "store": args.store,
            "product": args.product,
            "buyer": buyer.pubkey,
            "fee_payer": relay.pubkey,
            "table": args.table,
            "network": network,
            "rpc_url": args.rpc_url,
        },
        # The tool's default guard refuses loopback because the hosted surface is an
        # unauthenticated proxy. This runner is the operator's own process on the
        # operator's own machine, and a fork lives on loopback by construction, so on a
        # fork the guard is the scheme check alone. Mainnet keeps the public-only guard.
        url_guard=validate_rpc_url if network == "fork" else None,
    )
    if out.get("error"):
        print(f"STOP: {out['error']}")
        return 2
    if out.get("refused"):
        print(f"REFUSED [{out.get('code')}]: {out.get('reason')}")
        return 1
    gasless = out.get("gasless") or {}
    print(f"\n  RECEIPT  {out['status'].upper()}  {out['units_consumed']:,} CU")
    print(f"  binding  {out['binding_strength']}  {out.get('binding_prefix', '')}")
    print(f"  signers  {gasless.get('signatures_required')}")
    unsigned = out["transaction"]["unsigned_transaction"]

    if not args.broadcast:
        print(
            "\nDRY RUN: bytes prepared and verified; the relay was not asked to sign."
        )
        print("Re-run with --broadcast to sponsor, co-sign, and send.")
        return 0

    # 3. SETTLE: relay -> re-verify -> buyer (authority) -> merge -> send.
    plan = out["accounts"]
    entries = plan.values() if isinstance(plan, dict) else plan
    writable = frozenset(
        entry["address"]
        for entry in entries
        if entry.get("writable") and entry["address"] != buyer.pubkey
    )
    gate = SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=writable,
            sponsored=True,
            usdc_per_transaction_raw=int(round(args.max_spend_usdc * 1_000_000)),
        ),
        ledger=InMemorySpendLedger(),
    )
    signer = TransactionSigner(
        backend=buyer,
        profile=SignerProfile(
            name=(
                EXTERNAL_SIGNER_PROFILE_NAME
                if args.signer == "paybox"
                else DEVELOPER_KEYPAIR_FILE_PROFILE_NAME
            ),
            network=network,
            authorized=True,
            signing_as=AUTHORITY_ROLE,
        ),
        spend_gate=gate,
    )
    trace = Trace(lane="settle", network=str(network))
    outcome = settle_sponsored(
        unsigned,
        network=network,
        rpc_url=args.rpc_url,
        relay=relay,
        signer=signer,
        authority=buyer.pubkey,
        last_valid_block_height=int(out["expires"]["last_valid_block_height"]),
        trace=trace,
    )
    _emit_trace(args, trace)
    price_raw = _product_price_raw(args, network)
    stake = (
        (
            USDC_MINT,
            price_raw
            if price_raw is not None
            else int(round(args.max_spend_usdc * 1_000_000)),
            6,
        ),
    )
    if not isinstance(outcome, PurchaseSettled):
        print(f"\nREFUSED [{outcome.code}]: {outcome.reason}")
        _record_decision(
            network,
            lane="settle",
            programs=("let_me_buy",),
            terminal="refused",
            code=outcome.code,
            at_stake=stake,
            trace=trace,
        )
        return 1

    _record_decision(
        network,
        lane="settle",
        programs=("let_me_buy",),
        terminal="landed",
        signatures=(outcome.signature,),
        at_stake=stake,
        trace=trace,
    )
    print(f"\n  LANDED   {outcome.signature}")
    print(
        f"  CU       predicted {outcome.predicted_units}  charged {outcome.consumed_units}"
    )
    print(
        f"  signers  {outcome.signatures}  payer {outcome.fee_payer[:8]}…  "
        f"authority {outcome.authority[:8]}…"
    )

    # 4. JUDGE BY WHAT MOVED.
    buyer_sol_after = _balance(args.rpc_url, buyer.pubkey)
    relay_sol_after = _balance(args.rpc_url, relay.pubkey)
    print(f"\n  buyer SOL after    {buyer_sol_after}   (before {buyer_sol_before})")
    print(f"  relay SOL after    {relay_sol_after}   (before {relay_sol_before})")
    if buyer_sol_after != buyer_sol_before:
        print("NOT GASLESS: the buyer's SOL moved.")
        return 1
    print("GASLESS: the buyer's SOL did not move; the relay paid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
