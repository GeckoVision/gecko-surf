"""Ask Gecko what to do, then do it — the agent side of the routed purchase.

    uv run python scripts/buy_with_any_token.py --network fork --rpc-url http://... \
        --store jonasbar --product Water --keypair ~/.config/solana/gecko-buyer-mainnet.json

WHAT THIS IS, AND WHY IT IS A SCRIPT RATHER THAN A PACKAGE FUNCTION. Gecko decides and
hands back; it does not execute. That boundary is load-bearing — Orquestra builds the
transaction, a signer signs it, and the agent is the one that strings the steps together.
So the consumer of a route lives HERE, where the agent is, and `gecko/pay_route.py` stays
a thing that answers questions.

Every piece of this chain has run on mainnet. NONE of them has ever been driven by the
answer: the swap was run with an amount a human typed, and the purchase with a product a
human named. This is the first caller that asks `plan_payment` and then does what it says.

THE REFUSAL IS THE PRODUCT, NOT THE ERROR PATH. If the plan comes back blocked — a peg
that cannot be vouched for, a mint the storefront structurally cannot debit, a buyer whose
token account IS the store's — this stops and prints why. It does not retry, widen a cap,
or fall back to "pay directly and hope". A driver that routes around its own advisor is
worth less than no advisor.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gecko.networks import NETWORKS  # noqa: E402
from gecko.pay_route import plan_payment_result  # noqa: E402


def _run(argv: list[str]) -> int:
    print(f"\n  $ {' '.join(a if 'api-key' not in a else '<rpc>' for a in argv[3:])}")
    proc = subprocess.run(argv, cwd=str(ROOT), text=True)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rpc-url", required=True)
    p.add_argument("--network", required=True, choices=sorted(NETWORKS))
    p.add_argument("--store", required=True)
    p.add_argument("--product", required=True)
    p.add_argument("--buyer", required=True, help="the wallet that pays")
    p.add_argument("--keypair", required=True, help="signs both legs")
    p.add_argument("--table", type=int, default=1)
    p.add_argument(
        "--max-usdc-raw",
        type=int,
        default=200_000,
        help="the purchase leg's per-transaction cap, in raw units",
    )
    args = p.parse_args(argv)

    print("=" * 70)
    print("  ASK — what does Gecko say this wallet should do?")
    print("=" * 70)
    plan = plan_payment_result(
        {
            "store": args.store,
            "product": args.product,
            "buyer": args.buyer,
            "network": args.network,
            "rpc_url": args.rpc_url,
        }
    )
    if "error" in plan:
        print(f"  ERROR  {plan['error']}")
        return 1

    print(f"  outcome   {plan['outcome']}   blocked={plan['blocked']}")
    print(f"  reason    {plan['reason']}")
    for check in plan.get("peg_checks", []):
        print(
            f"  peg       {check['side']:11} {check['mint'][:10]}…  "
            f"{check['outcome']:12} blocks={check['blocks']}"
        )

    if plan["blocked"]:
        print("\n  STOPPED — Gecko refused, so this driver refuses. The refusal IS the")
        print("  answer; routing around it would make the advice decorative.")
        return 2

    if plan["outcome"] == "payable_now":
        print("\n  no conversion needed — buying directly")
    else:
        route = plan["route"]
        quote = route["quote"]
        print(
            f"\n  route     convert {quote['amount_in']} of {route['held_mint'][:10]}…"
        )
        print(f"            at pool {quote['pool']}  direction {quote['direction']}")
        print(f"            sized under a {quote['slippage_bps']} bps bound")
        print("\n" + "=" * 70)
        print("  ACT 1 — the conversion Gecko named, with the amount Gecko sized")
        print("=" * 70)
        rc = _run(
            [
                "uv",
                "run",
                "python",
                "scripts/prepare_whirlpool_swap.py",
                "--signer",
                args.buyer,
                "--keypair",
                args.keypair,
                # Taken from the plan, never typed: the amount is the whole point.
                "--direction",
                quote["direction"].replace("_", "-"),
                "--amount",
                str(quote["amount_in"]),
                "--rpc-url",
                args.rpc_url,
                "--network",
                args.network,
                "--send",
            ]
        )
        if rc != 0:
            print("\n  the conversion did not complete — stopping before the purchase")
            return rc

    print("\n" + "=" * 70)
    print("  ACT 2 — the purchase")
    print("=" * 70)
    return _run(
        [
            "uv",
            "run",
            "python",
            "scripts/autonomous_purchase.py",
            "--network",
            args.network,
            "--rpc-url",
            args.rpc_url,
            "--keypair",
            args.keypair,
            "--store",
            args.store,
            "--product",
            args.product,
            "--table",
            str(args.table),
            "--max-usdc-raw",
            str(args.max_usdc_raw),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
