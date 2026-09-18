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
    settle_sponsored,
)
from gecko.networks import coerce_network  # noqa: E402
from gecko.prepare_purchase import prepare_purchase_result  # noqa: E402
from gecko.rpc import default_rpc_call, validate_rpc_url  # noqa: E402
from gecko.sandbox import ephemeral_signer, prove_surfnet  # noqa: E402
from gecko.sandbox.cheatcodes import fund_sol  # noqa: E402
from gecko.sandbox.rehearse import rehearse_purchase  # noqa: E402
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
    if not isinstance(outcome, PurchaseSettled):
        print(f"\nREFUSED [{outcome.code}]: {outcome.reason}")
        return 1

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
