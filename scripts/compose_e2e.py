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
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.txbind import _b58decode, evaluate_tx  # noqa: E402
from scripts.prepare_purchase import USDC, build_instruction, derive_ata  # noqa: E402


def _rpc(url: str, method: str, params: list) -> dict:
    return default_rpc_call(url, method, params)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signer", required=True, help="Public key only.")
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--product", default="Water")
    parser.add_argument("--table", type=int, default=11)
    args = parser.parse_args(argv)

    print("1. GECKO — derive the accounts this call needs")
    ata = derive_ata(args.signer, USDC)
    print(f"   signer       {args.signer}")
    print(f"   USDC account {ata}   (derived, not looked up)")

    print("\n2. ORQUESTRA — build the transaction")
    built = build_instruction(args.signer, ata, args.product, args.table)
    b58 = built.get("serializedTransaction")
    if not b58:
        print("   STOP: builder returned no serializedTransaction")
        return 2
    print(f"   encoding     {built.get('encoding')}")
    print(f"   build check  simulationError={built.get('simulationError')}")

    blockhash, valid_until = latest_blockhash(args.rpc_url, _rpc)
    print(f"   blockhash    {blockhash}  (valid to height {valid_until})")

    print("\n3. GECKO — simulate against LIVE mainnet, read-only")
    receipt = simulate(
        {},
        rpc_url=args.rpc_url,
        rpc_call=_rpc,
        build_call=lambda _plan: BuiltTx(tx=b58, encoding="base58"),
        replace_blockhash=False,
        network_label="live mainnet, read-only, unsigned",
        track=[args.signer],
    )
    units = f"{receipt.units_consumed:,} CU" if receipt.units_consumed else "—"
    print(f"   RECEIPT      {receipt.status.upper()}   {units}")
    if receipt.revert_class:
        print(f"   class        {receipt.revert_class}")

    verdict = evaluate_tx(b58, receipt, encoding="base58", require="exact")
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
