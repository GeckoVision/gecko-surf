"""Can this wallet buy this product — and if not, what is the shortest honest route?

    uv run python scripts/pay_with_any_token.py --signer <PUBKEY> \
        --store geckocoffee --product Espresso

THIS FILE OWNS NO DECISIONS. It calls :func:`gecko.pay_route.plan_payment_result` and
prints what came back. Every judgment — what the wallet holds, whether the priced mint can
be spent through the program at all, whether the buyer is the store, which venue trades the
pair, whether a peg blocks a leg, how large the input must be — lives in the package, where
an agent can reach it too.

WHY THAT MATTERS MORE THAN TIDINESS. This logic used to live HERE, inline, and the package
grew its own copy. Two implementations of one question drift, and the drift is invisible
until a mainnet run disagrees with a tool: the founder reads this report before spending
real money, and an agent reads `plan_payment` before doing the same thing autonomously. If
those two ever answer differently, the one that is wrong is whichever nobody was watching.

So this script and the agent-facing tool now run the SAME function over the SAME chain
reads. The report below is a rendering, not a second opinion.

THE SLIPPAGE BOUND IS NOT A FLAG ANY MORE, and its removal is the point. This script used
to take `--slippage-bps` and size against it while `prepare_whirlpool_swap.py` built the
swap under its own default. Two numbers for one guarantee is how a sizing that looked
correct printed 101,001 against a 100,000 price where 101,022 was needed — it cleared with
1,011 raw to spare, on the FILL rather than on the floor, which is luck wearing the shape
of a guarantee. `gecko.pay_route.SWAP_SLIPPAGE_BPS` is now the single number, pinned by a
test against the builder's declared default. A flag here could only reintroduce the bug.

Nothing in this file signs, and nothing it prints is a transaction.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.pay_route import SWAP_SLIPPAGE_BPS, plan_payment_result  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402

#: Retry policy is the CALLER's to choose, which is why the package takes the call as a
#: seam. A public endpoint 429s under any real use and a half-finished read here would
#: read as "this wallet holds nothing", which is the one answer that must never be
#: manufactured by a transport failure.
_TRIES = 4


def _retrying_call(rpc_url: str, method: str, params: list) -> dict:
    import time

    last: Exception | None = None
    for attempt in range(_TRIES):
        try:
            return default_rpc_call(rpc_url, method, params)
        except Exception as exc:  # noqa: BLE001 - transport, retried then surfaced
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"rpc {method} failed after {_TRIES} tries: {last}")


def _print_report(report: dict, store: str, product: str) -> None:
    print(f"WANT   {product} at {store}")
    print(f"  price        {report['price_raw']} raw")
    print(f"  priced mint  {report['priced_mint']}")
    print(f"  under        {report['priced_program']}")
    # A structural fact about the program, not a preference: make_purchase PINS this in
    # its IDL, so a mint under any other token program cannot be spent here at all.
    match = report["priced_program"] == report["pinned_program"]
    print(
        f"  make_purchase pins {report['pinned_program']}"
        f"  -> {'match' if match else 'MISMATCH'}"
    )

    holdings = report.get("holdings") or {}
    print(f"\nHAVE   {report['buyer']}")
    for mint, amount in sorted(holdings.items(), key=lambda kv: -int(kv[1])):
        tag = "  <- the priced mint" if mint == report["priced_mint"] else ""
        print(f"  {int(amount):>14,}  {mint}{tag}")
    if not holdings:
        print("  (no token balances)")

    for check in report.get("peg_checks") or ():
        mark = {"ok": "holding", "refuse": "NOT HOLDING", "unknown": "not tracked"}.get(
            check.get("outcome", "unknown"), check.get("outcome", "?")
        )
        label = check.get("symbol") or (check.get("mint") or "")[:8]
        print(f"  peg check    {label}: {mark} — {check.get('reason', '')}")

    print()
    outcome = report["outcome"]

    if outcome == "payable_now":
        print(f"PAYABLE NOW — {report['reason']}")
        print(
            f"  uv run python scripts/prepare_purchase.py --signer {report['buyer']} \\"
        )
        print(f"    --store {store} --product '{product}'")
        return

    if outcome == "route_found":
        route = report["route"] or {}
        print("ROUTE — derived from the mint pair, not chosen:")
        print(f"  swap {route.get('input_mint')}")
        print(
            f"    venue        {route.get('pool')}  "
            f"(tick_spacing {route.get('tick_spacing')}, "
            f"liquidity {route.get('liquidity')})"
        )
        print(
            "    proven by    re-deriving its own address from its config + mints + tick_spacing"
        )
        print(f"    direction    {route.get('direction')}")
        # Read the bound OFF the quote. Hardcoding it here would recreate the two-numbers
        # bug from the other side.
        bound = route.get("slippage_bps", SWAP_SLIPPAGE_BPS)
        print(
            f"    spend        {route.get('amount_in')} raw, sized at {bound} bps — the "
            "same bound the swap is BUILT with"
        )
        print("\n  Then, in order:")
        print(
            f"    uv run python scripts/prepare_whirlpool_swap.py --signer {report['buyer']} \\"
        )
        print(
            f"      --direction {route.get('direction')} "
            f"--amount {route.get('amount_in')} --keypair <KEY> --send"
        )
        print(
            f"    uv run python scripts/prepare_purchase.py --signer {report['buyer']} \\"
        )
        print(f"      --store {store} --product '{product}'")
        return

    # Everything else is a refusal, and the package already wrote the sentence.
    print(f"NOT PAYABLE ({outcome}) — {report['reason']}")
    for leg in report.get("rejected_legs") or ():
        print(f"  rejected     {leg.get('input_mint')} via {leg.get('pool')}")
    if report.get("no_pool_for"):
        print(f"  no pool for  {', '.join(report['no_pool_for'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pay_with_any_token",
        description=(
            "Can this wallet buy this product, and if not, what is the route? "
            "Signs nothing, builds nothing, spends nothing."
        ),
    )
    parser.add_argument("--signer", required=True, help="the buyer's base58 pubkey")
    parser.add_argument("--store", default="geckocoffee")
    parser.add_argument("--product", default="Espresso")
    parser.add_argument("--network", default="mainnet", help="asserted, never inferred")
    parser.add_argument("--rpc-url", default=None, help="override the network's RPC")
    args = parser.parse_args(argv)

    request: dict[str, object] = {
        "store": args.store,
        "product": args.product,
        "buyer": args.signer,
        "network": args.network,
    }
    if args.rpc_url:
        request["rpc_url"] = args.rpc_url

    report = plan_payment_result(request, rpc_call=_retrying_call)

    if "error" in report:
        print(f"pay_with_any_token: {report['error']}", file=sys.stderr)
        return 2

    _print_report(report, args.store, args.product)
    # `blocked` is the package's own verdict. Re-deriving it from the outcome string here
    # would be a second copy of the rule that decides whether to stop.
    return 1 if report.get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
