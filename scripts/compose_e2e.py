"""The three-party loop, end to end: Gecko verifies · Orquestra builds · a signer signs.

    Gecko        plan the call, simulate against LIVE mainnet, bind the receipt to the
                 exact message
    Orquestra    POST /instructions/make_purchase/build -> serializedTransaction
    signer       @orquestradev/signer-mcp `sign_transaction` (base64 wire bytes)

This script runs everything up to the signature and stops. It never signs and never
broadcasts — it prints the base64 the signer takes, and the binding the signer should be
made to check.

    uv run python scripts/compose_e2e.py --signer <PUBKEY> --rpc-url <MAINNET>

**The bridge this exists to prove.** Orquestra returns `serializedTransaction` **base58**;
`sign_transaction` takes **base64**. Nothing in either tool converts, so an integration
that assumes one encoding fails at deserialization with an error that points nowhere near
the cause. We hit that exact class twice in one day.

**The gap this exists to make visible.** `sign_and_send_transaction` signs and broadcasts
whatever it is handed. Custody backends — Turnkey, Privy, AWS KMS, Fireblocks — protect
the KEY; none of them will refuse a well-formed transaction that does the wrong thing.
The receipt binding printed below is the thing that could, if the signer checked it.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.landing import latest_blockhash  # noqa: E402
from gecko.networks import NETWORKS, coerce_network  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.txbind import _b58decode, evaluate_tx  # noqa: E402
from gecko.store_accounts import (  # noqa: E402
    StoreResolutionError,
    derive_ata,
    resolve_store,
)
from scripts.prepare_purchase import DEFAULT_STORE, build_instruction  # noqa: E402


def _rpc(url: str, method: str, params: list) -> dict:
    return default_rpc_call(url, method, params)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signer", required=True, help="Public key only.")
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument(
        "--store",
        default=DEFAULT_STORE,
        help=(
            "which storefront to buy from, by name — it selects that store's accounts "
            "too, read from its own on-chain account."
        ),
    )
    parser.add_argument("--product", default="Water")
    parser.add_argument("--table", type=int, default=11)
    parser.add_argument(
        "--network",
        required=True,
        choices=sorted(NETWORKS),
        help=(
            "Which network --rpc-url points at. YOU say; nothing here guesses. A fork "
            "proxy answers at any hostname, so the URL is evidence of nothing. There is "
            "no default: 'unknown' is a legal answer and refuses at the gate."
        ),
    )
    args = parser.parse_args(argv)
    # `choices` already closed the set; this narrows the type for the two call sites
    # below without a cast, and stays fail-closed if the flag is ever widened.
    network = coerce_network(args.network)

    print("1. GECKO — derive the accounts this call needs")
    try:
        store = resolve_store(
            args.store, rpc_url=args.rpc_url, rpc_call=default_rpc_call
        ).accounts_for(args.product)
    except StoreResolutionError as exc:
        print(f"   STOP: {exc}")
        return 2
    ata = derive_ata(args.signer, store.mint)
    print(f"   signer       {args.signer}")
    print(f"   store        {store.store_name}  paying {store.token_account}")
    print(f"   token acct   {ata}   (derived, not looked up)")

    print("\n2. ORQUESTRA — build the transaction")
    built = build_instruction(store, args.signer, ata, args.table)
    b58 = built.get("serializedTransaction")
    if not b58:
        print("   STOP: builder returned no serializedTransaction")
        return 2
    print(f"   encoding     {built.get('encoding')}")
    print(f"   build check  simulationError={built.get('simulationError')}")

    blockhash, valid_until = latest_blockhash(args.rpc_url, _rpc)
    print(f"   blockhash    {blockhash}  (valid to height {valid_until})")

    print(f"\n3. GECKO — simulate against {network}, read-only")
    receipt = simulate(
        {},
        rpc_url=args.rpc_url,
        rpc_call=_rpc,
        build_call=lambda _plan: BuiltTx(tx=b58, encoding="base58"),
        replace_blockhash=False,
        network_label=f"{network} snapshot, read-only, unsigned (operator-asserted)",
        network=network,
        track=[args.signer],
    )
    units = f"{receipt.units_consumed:,} CU" if receipt.units_consumed else "—"
    print(f"   RECEIPT      {receipt.status.upper()}   {units}")
    print(f"   network      {receipt.network}   (as you stated it, never inferred)")
    if receipt.revert_class:
        print(f"   class        {receipt.revert_class}")

    # The SAME flag on both sides, and that is correct rather than tautological here:
    # these bytes are going to be signed and submitted to THIS `--rpc-url`, so "where the
    # simulation ran" and "where the signature is headed" are one fact, stated once, by
    # the operator. What it is NOT is a check that the operator was right — say `mainnet`
    # while pointed at a fork and this script believes you. The refusal it does buy is
    # the one that matters at the seam: a receipt taken elsewhere, or one that named no
    # network at all, cannot clear this signature.
    verdict = evaluate_tx(
        b58, receipt, encoding="base58", require="exact", expected_network=network
    )
    print(f"   binding      {verdict.approved}: {verdict.reason}")
    if not verdict.approved:
        print("\n   DO NOT SIGN — the pre-flight did not approve these bytes.")
        return 1

    # base58 -> base64. Neither tool does this, and an integration that assumes one
    # encoding fails at deserialization, far from the cause.
    raw = _b58decode(b58)
    b64 = base64.b64encode(raw).decode()

    print("\n4. SIGNER — what to hand @orquestradev/signer-mcp")
    print("   tool         sign_transaction")
    print(f"   transaction  <base64, {len(b64)} chars>")
    print(f"   binding      {receipt.message_binding}  [{receipt.binding_strength}]")
    print(
        "\n   The signer will sign whatever it is given. A custody backend protects the\n"
        "   KEY, not the TRANSACTION — so the binding above is the only thing that ties\n"
        "   these bytes to a receipt that passed. Nothing here signs or broadcasts."
    )
    print(f"\n{json.dumps({'transaction': b64}, indent=1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
