"""One call: buy from geckocoffee with whatever the wallet actually holds.

    # what would happen, spending nothing (THE DEFAULT)
    uv run python scripts/geckocoffee_chain.py --product "Sparkling water"

    # spend real money on mainnet
    uv run python scripts/geckocoffee_chain.py --product "Sparkling water" \
        --broadcast --max-spend-usdc 0.10

WHAT THIS IS. An orchestrator, not a signer. It reads the wallet, asks
`plan_payment` for a route, and when a conversion is needed drives the two paths that
already exist and are already audited:

    convert   scripts/prepare_whirlpool_swap.py --send
    purchase  scripts/autonomous_purchase.py

`scripts/sign_and_send.py` is the only file in this repository that can sign a mainnet
transaction, and this file does not become the second one. It shells out. Under time
pressure the temptation is to inline "just the signing part"; a second signing path is
exactly the thing that later disagrees with the first about a spend cap.

THREE RAILS, and they hold even when the founder has authorised the run:

  1. `--broadcast` is required to spend anything. Without it every leg is prepared and
     simulated and nothing is signed. A script whose default spends money is a script
     that spends money by accident.
  2. `--max-spend-usdc` is a CEILING, not a suggestion. The product's own price is read
     off the chain and compared to it before a single transaction is built. A price that
     moved is a refusal, not a surprise.
  3. The balances are read before and after and the verdict is WHAT MOVED — never the
     exit code of the last command. A leg that returned 0 and moved nothing is a failure
     this will name.

WHY BALANCES AND NOT RETURN CODES. `gecko.effects` makes the same argument for the same
reason: a transaction that "succeeded" and moved nothing is the interesting failure, and
only the ledger knows. Every number printed at the end is a difference between two reads
of mainnet, not a claim any of these scripts made about itself.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gecko.rpc import default_rpc_call  # noqa: E402

USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
STORE = "geckocoffee"


def _balances(rpc_url: str, owner: str) -> dict[str, Any]:
    """SOL and both mints, read off mainnet. The only source of truth here."""
    out: dict[str, Any] = {}
    out["sol"] = (
        default_rpc_call(rpc_url, "getBalance", [owner]).get("result") or {}
    ).get("value", 0)
    for name, mint in (("usdg", USDG), ("usdc", USDC)):
        rows = (
            default_rpc_call(
                rpc_url,
                "getTokenAccountsByOwner",
                [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
            ).get("result")
            or {}
        ).get("value") or []
        out[name] = sum(
            int(r["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            for r in rows
        )
    return out


def _settled_balances(
    rpc_url: str,
    owner: str,
    before: dict[str, Any],
    *,
    tries: int = 12,
    pause: float = 2.0,
) -> tuple[dict[str, Any], bool]:
    """Read balances until they stop agreeing with `before`, or give up saying so.

    THE RACE THIS CLOSES, measured on a real mainnet run. Both legs settled — the swap
    at slot 447328207 and the purchase at 447328217, both `err: None` — and the very
    next balance read came back byte-identical to the opening one, so the script
    announced NOTHING MOVED over a chain where 0.040409 USDG had just moved.

    A confirmed transaction and an indexed account are different events. `getBalance`
    and `getTokenAccountsByOwner` answer from whatever slot the node has processed, and
    that can trail a confirmation by a second or two. Reading once, immediately, asks the
    question before the node can answer it.

    Returns `(balances, moved)`. `moved is False` after every attempt is an HONEST "the
    ledger still shows nothing", which is a real failure worth reporting — it is only
    indistinguishable from the race if you never wait.
    """
    import time

    latest = before
    for attempt in range(tries):
        latest = _balances(rpc_url, owner)
        if latest != before:
            if attempt:
                _say(f"    (ledger caught up after {attempt * pause:.0f}s)")
            return latest, True
        time.sleep(pause)
    return latest, False


def _say(line: str = "") -> None:
    print(line, flush=True)


def _redact(argv: list[str]) -> list[str]:
    """The RPC URL carries an API key. This output is read over a shoulder and recorded,
    so the key never reaches the terminal — the same rule the rest of the repo follows
    about never logging a credential, applied to the one place it would leak by echo."""
    out, hide = [], False
    for part in argv:
        if hide:
            out.append("<rpc-url redacted>")
            hide = False
            continue
        if part == "--rpc-url":
            hide = True
        out.append(part)
    return out


def _run(argv: list[str], *, dry: bool) -> int:
    """Drive one of the audited legs. Never captures output — the operator watches it."""
    shown = _redact(argv)
    _say("    $ python " + " ".join(shown[1:]))
    if dry:
        _say("      (not run — no --broadcast)")
        return 0
    return subprocess.call(argv, cwd=str(ROOT))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="geckocoffee_chain",
        description="Buy from geckocoffee with whatever the wallet holds. Prepares and "
        "simulates by default; --broadcast to spend.",
    )
    p.add_argument("--product", default="Sparkling water")
    p.add_argument("--buyer", default="9cJbQKxxqCbumpoeb7YWC3QESzFD8LxpbHVAXrTUsPfh")
    p.add_argument(
        "--keypair", default=str(Path.home() / ".gecko/wallets/demo-buyer.json")
    )
    p.add_argument("--rpc-url", default=os.environ.get("RPC", ""))
    p.add_argument("--broadcast", action="store_true", help="actually spend money")
    p.add_argument(
        "--max-spend-usdc",
        type=float,
        default=0.10,
        help="ceiling on the product price, in USDC. Refuses above it.",
    )
    args = p.parse_args(argv)

    if not args.rpc_url:
        _say("no RPC url: pass --rpc-url or export RPC")
        return 2
    dry = not args.broadcast

    _say("=" * 64)
    _say(f"  geckocoffee · {args.product}" + ("" if args.broadcast else "   [DRY RUN]"))
    _say("=" * 64)

    before = _balances(args.rpc_url, args.buyer)
    _say("\n  wallet before")
    _say(f"    USDG  {before['usdg'] / 1e6:.6f}")
    _say(f"    USDC  {before['usdc'] / 1e6:.6f}")
    _say(f"    SOL   {before['sol'] / 1e9:.9f}")

    # 1. THE ROUTE. Asked before anything is built, so a refusal costs nothing.
    from gecko.pay_route import plan_payment_result

    _say("\n  1. plan_payment")
    plan = plan_payment_result(
        {
            "store": STORE,
            "product": args.product,
            "buyer": args.buyer,
            "network": "mainnet",
            "rpc_url": args.rpc_url,
        }
    )
    outcome = plan.get("outcome")
    _say(f"    outcome  {outcome}")
    _say(f"    reason   {(plan.get('reason') or '')[:110]}")

    if outcome in {
        "no_route",
        "no_candidates",
        "self_purchase",
        "pinned_program_mismatch",
    }:
        _say("\n  REFUSED before spending anything. That is the gate working.")
        return 1

    # 2. THE CEILING. Read off the STORE ACCOUNT, because `plan_payment` reports a route
    #    and not a price, and a cap checked against a number we assumed is not a cap.
    from gecko.store_directory import list_stores_result

    price_ui = None
    listing = list_stores_result({"network": "mainnet", "rpc_url": args.rpc_url})
    for store in listing.get("stores") or []:
        if store.get("store") != STORE:
            continue
        for item in store.get("products") or []:
            if item.get("name") == args.product:
                price_ui = float(item["price_ui"])
                break
    if price_ui is not None:
        _say(
            f"\n  2. spend ceiling   price {price_ui:.6f} USDC  ·  cap {args.max_spend_usdc:.6f}"
        )
        if price_ui > args.max_spend_usdc:
            _say("    REFUSED — the price is above the cap you set. Nothing was built.")
            return 1
    else:
        _say(
            "\n  2. spend ceiling   price not reported by the planner; cap not enforceable"
        )
        if args.broadcast:
            _say(
                "    REFUSED — will not broadcast without a price to check the cap against."
            )
            return 1

    route = plan.get("route") or {}
    quote = route.get("quote") or {}

    # 3. THE CONVERT LEG, only when the planner said one is needed.
    if outcome.startswith("route_found"):
        _say("\n  3. convert leg")
        _say(f"    venue    {quote.get('venue')}  ·  curve {quote.get('curve')}")
        _say(f"    pool     {quote.get('pool')}")
        _say(f"    amount   {quote.get('amount_in')} USDG in")
        rc = _run(
            [
                sys.executable,
                "scripts/prepare_whirlpool_swap.py",
                "--signer",
                args.buyer,
                "--rpc-url",
                args.rpc_url,
                "--network",
                "mainnet",
                "--amount",
                str(quote.get("amount_in")),
                # THE DIRECTION IS NOT OPTIONAL. Left to the script's default this ran
                # b-to-a — USDC into USDG, the exact opposite of the route the planner
                # found — and the pre-flight refused it with `insufficient funds`
                # because the wallet holds 0.01 USDC and was asked to spend 0.040409.
                # The planner already knows which way round it is; pass it.
                "--direction",
                "a-to-b" if quote.get("direction") == "a_to_b" else "b-to-a",
                *(["--send", "--keypair", args.keypair] if args.broadcast else []),
            ],
            dry=dry,
        )
        if rc != 0:
            _say(f"\n  convert leg exited {rc} — stopping before the purchase.")
            return rc
    else:
        _say(
            "\n  3. convert leg   not needed — the wallet already holds the priced mint"
        )

    # 4. THE PURCHASE.
    _say("\n  4. purchase")
    rc = _run(
        [
            sys.executable,
            "scripts/autonomous_purchase.py",
            "--network",
            "mainnet",
            "--rpc-url",
            args.rpc_url,
            "--keypair",
            args.keypair,
            "--store",
            STORE,
            "--product",
            args.product,
        ],
        dry=dry,
    )
    if rc != 0:
        _say(f"\n  purchase exited {rc}")
        return rc

    # 5. THE VERDICT: what MOVED. Not what any command returned.
    # Wait for the node to index what it just confirmed — see _settled_balances.
    if dry:
        after, moved = _balances(args.rpc_url, args.buyer), False
    else:
        after, moved = _settled_balances(args.rpc_url, args.buyer, before)
    _say("\n  wallet after")
    _say(
        f"    USDG  {after['usdg'] / 1e6:.6f}   ({(after['usdg'] - before['usdg']) / 1e6:+.6f})"
    )
    _say(
        f"    USDC  {after['usdc'] / 1e6:.6f}   ({(after['usdc'] - before['usdc']) / 1e6:+.6f})"
    )
    _say(
        f"    SOL   {after['sol'] / 1e9:.9f}   ({(after['sol'] - before['sol']) / 1e9:+.9f})"
    )

    if dry:
        _say(
            "\n  DRY RUN — nothing was signed and nothing moved. Re-run with --broadcast."
        )
        return 0

    if not moved:
        _say("\n  NOTHING MOVED, after waiting for the node to catch up. Every leg")
        _say("  returned 0 and the ledger still disagrees — treat this as a failure,")
        _say("  not a success, and read the leg output above.")
        return 1
    _say("\n  done — judged by what moved, not by an exit code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
