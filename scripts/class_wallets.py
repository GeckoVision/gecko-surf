"""Founder-side: list the registered class wallets and what to send each one.

    uv run python scripts/class_wallets.py list --cohort 2026-09
    uv run python scripts/class_wallets.py list --cohort 2026-09 --json export.json
    uv run python scripts/class_wallets.py revoke --account <id> --cohort 2026-09

`list` reads MONGODB_URI from the process environment (or a `--json` export: a JSON
array, `{"wallets": [...]}`, or mongoexport JSON lines) and prints one row per
registered wallet with the USDC raw amount and SOL lamports to send, then the totals.

This script NEVER signs or sends anything. It prints what to send; the founder sends
it from their own wallet. Every address listed was registered with an ed25519
signature over a server-issued challenge, so its owner controls it
(see gecko/registry/class_wallets.py).

Defaults: 3 espressos at 100000 raw (0.10 USDC) = 300000 raw, plus 9400000 lamports
(0.0094 SOL) for fees and the first purchase's rent for the merchant's token account.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from gecko.registry.class_wallets import FundingPlan, FundingRow, funding_plan

USDC_DECIMALS = 6
SOL_DECIMALS = 9


def _mongo_db() -> Any:
    uri = (os.environ.get("MONGODB_URI") or "").strip()
    if not uri:
        raise SystemExit("MONGODB_URI is not set in this shell (or pass --json).")
    from pymongo import MongoClient

    return MongoClient(uri, serverSelectionTimeoutMS=5000)["gecko_registry"]


def _load_export(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        data = data.get("wallets", [])
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a list of wallet documents")
    return [d for d in data if isinstance(d, dict)]


def _rows(docs: list[dict[str, Any]], cohort: str) -> list[FundingRow]:
    picked = [
        FundingRow(account=str(d["account"]), address=str(d["address"]))
        for d in docs
        if d.get("cohort") == cohort and d.get("account") and d.get("address")
    ]
    return sorted(picked, key=lambda r: r.account)


def _units(raw: int, decimals: int) -> str:
    return f"{raw / 10**decimals:.{decimals}f}"


def render(plan: FundingPlan, cohort: str) -> str:
    lines = [
        f"cohort {cohort}: {len(plan.rows)} registered wallet(s)",
        f"{'account':<28} {'address':<44} {'usdc_raw':>10} {'sol_lamports':>13}",
    ]
    for r in plan.rows:
        lines.append(
            f"{r.account:<28} {r.address:<44} {r.usdc_raw:>10} {r.sol_lamports:>13}"
        )
    lines += [
        "",
        f"total usdc_raw     {plan.total_usdc_raw}"
        f"  ({_units(plan.total_usdc_raw, USDC_DECIMALS)} USDC)",
        f"total sol_lamports {plan.total_sol_lamports}"
        f"  ({_units(plan.total_sol_lamports, SOL_DECIMALS)} SOL)",
        "",
        "Nothing was sent. Send these amounts from your own wallet.",
    ]
    return "\n".join(lines)


def _list(args: argparse.Namespace) -> int:
    if args.json:
        docs = _load_export(Path(args.json))
    else:
        docs = list(
            _mongo_db()["class_wallets"].find({"cohort": args.cohort}, {"_id": 0})
        )
    plan = funding_plan(
        _rows(docs, args.cohort),
        price_raw=args.price_raw,
        count=args.count,
        sol_lamports=args.sol_lamports,
    )
    print(render(plan, args.cohort))
    return 0


def _revoke(args: argparse.Namespace) -> int:
    result = _mongo_db()["class_wallets"].delete_one(
        {"account": args.account, "cohort": args.cohort}
    )
    if int(getattr(result, "deleted_count", 0)) == 0:
        print(f"no registration for {args.account} in cohort {args.cohort}")
        return 1
    print(f"revoked {args.account} in cohort {args.cohort}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="print what to send each registered wallet")
    listing.add_argument("--cohort", required=True)
    listing.add_argument("--price-raw", type=int, default=100_000)
    listing.add_argument("--count", type=int, default=3)
    listing.add_argument("--sol-lamports", type=int, default=9_400_000)
    listing.add_argument("--json", help="read an export instead of MONGODB_URI")
    listing.set_defaults(func=_list)

    revoke = sub.add_parser("revoke", help="delete one registration")
    revoke.add_argument("--account", required=True)
    revoke.add_argument("--cohort", required=True)
    revoke.set_defaults(func=_revoke)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
