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
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gecko.networks import NETWORKS  # noqa: E402
from gecko.pay_route import plan_payment_result  # noqa: E402
from gecko.peg_guard import PegReading, verdict_from_reading  # noqa: E402
from gecko.pegana import pegana_reader  # noqa: E402


def _run(argv: list[str]) -> tuple[int, str]:
    print(
        f"\n  $ {' '.join(a if 'api-key' not in a else '<rpc>' for a in argv[3:])}",
        flush=True,
    )
    # Captured, then echoed. The first mainnet run interleaved child output with the
    # parent's buffered prints, and the report read out of order — and the SENT
    # signature the driver needed was on a terminal, not in a variable.
    proc = subprocess.run(argv, cwd=str(ROOT), text=True, capture_output=True)
    print(proc.stdout, flush=True)
    if proc.returncode != 0 and proc.stderr:
        print(proc.stderr[-600:], flush=True)
    return proc.returncode, proc.stdout


def _confirm_signature(rpc_url: str, signature: str, *, timeout_s: int = 75) -> bool:
    """Poll until the signature CONFIRMS — the swap script exits 0 at BROADCAST.

    Its own last line says "verify it on chain before believing this line", and the
    first mainnet run of this driver is what happens when the driver believes it anyway:
    the purchase raced the swap, simulated against un-swapped balances, and refused
    insufficient_funds — a correct-looking refusal whose real cause was the invocation.
    """
    import time

    from gecko.rpc import default_rpc_call

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = (
            default_rpc_call(rpc_url, "getSignatureStatuses", [[signature]])
            .get("result", {})
            .get("value", [None])
        )[0]
        if value and value.get("err") is not None:
            print(
                f"  conversion LANDED AND FAILED on chain: {value['err']}", flush=True
            )
            return False
        if value and value.get("confirmationStatus") in ("confirmed", "finalized"):
            return True
        time.sleep(2)
    print(
        f"  conversion NOT CONFIRMED within {timeout_s}s — signature {signature}\n"
        "  look it up before retrying: a broadcast that lands late still spends.",
        flush=True,
    )
    return False


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
        "--accept-stale-peg",
        action="store_true",
        help=(
            "Proceed when the ONLY thing blocking the route is a peg oracle whose "
            "reading is STALE — the operator asserting the peg on their own authority. "
            "Narrow on purpose: a DEPEG, CRITICAL or any non-stale refusal still stops, "
            "flag or no flag. The live verdicts are printed either way, so the override "
            "is visible in the artifact rather than smoothed into an 'ok'."
        ),
    )
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
    peg_reader = None
    if args.accept_stale_peg:
        # Decided from STRUCTURED verdict fields, never by parsing a reason string —
        # prose must not be load-bearing. It fabricates nothing silently either: each
        # live reading is fetched and PRINTED first; only a verdict that is
        # refuse-BECAUSE-STALE (state UNKNOWN, stale=True) is replaced, and the
        # replacement's state_reason names the override and the flag, so the plan's own
        # peg_checks carry the acknowledgment into the record. Anything else — a DEPEG,
        # a CRITICAL, an unreachable oracle — passes through and still blocks.
        live = pegana_reader()

        def _acknowledged(mint: str) -> PegReading:
            reading = live(mint)
            verdict = verdict_from_reading(mint, reading)
            print(
                f"  live peg  {mint[:10]}…  {verdict.outcome:12} "
                f"state={verdict.state} stale={verdict.stale}"
            )
            if verdict.blocks and verdict.stale and verdict.state in (None, "UNKNOWN"):
                print(
                    "            ^ STALE-ONLY refusal — overridden by the operator "
                    "(--accept-stale-peg)"
                )
                return PegReading(
                    tracked=True,
                    symbol=reading.symbol,
                    state_body={
                        "state": "PEGGED",
                        "stale": False,
                        "state_reason": (
                            "operator-override --accept-stale-peg: the oracle reading "
                            "is stale and the operator asserted the peg on their own "
                            "authority"
                        ),
                    },
                )
            return reading

        peg_reader = _acknowledged

    plan = plan_payment_result(
        {
            "store": args.store,
            "product": args.product,
            "buyer": args.buyer,
            "network": args.network,
            "rpc_url": args.rpc_url,
        },
        peg_reader=peg_reader,
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
        swap_result = _run(
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
        rc, swap_out = swap_result
        if rc != 0:
            print("\n  the conversion did not complete — stopping before the purchase")
            return rc
        sent = re.search(r"SENT\s+(\S{40,})", swap_out)
        if not sent:
            print("\n  no SENT signature in the swap output — stopping")
            return 1
        print(
            f"\n  confirming the conversion ({sent.group(1)[:20]}…) before buying with it",
            flush=True,
        )
        if not _confirm_signature(args.rpc_url, sent.group(1)):
            return 1

    print("\n" + "=" * 70)
    print("  ACT 2 — the purchase")
    print("=" * 70)
    rc2, _out = _run(
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
    return rc2


if __name__ == "__main__":
    raise SystemExit(main())
