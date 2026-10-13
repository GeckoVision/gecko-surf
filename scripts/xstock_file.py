"""The living file for one tokenized stock (xStock): plan free, then run.

Thin CLI over gecko.livingfile. Free reads: the mint on Solana RPC, Jupiter's token
price and liquidity, two legal pages. One paid read: the real share price through
Orthogonal (financial-datasets /prices/snapshot), capped at $0.05 a run, $0.20 in all.

    # $0, made-up values (writes to ~/gecko-lists/):
    uv run python scripts/xstock_file.py --mode recorded

    # live: prints the plan (free) and stops
    uv run python scripts/xstock_file.py --mode live --mint Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh
    # live: plan, then read and pay
    uv run python scripts/xstock_file.py --mode live --mint Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh --yes

    # the stored file, and what changed since a run (free, no call)
    uv run python scripts/xstock_file.py --mode live --show
    uv run python scripts/xstock_file.py --mode live --changes-since 20261008-120000

The Orthogonal key comes from the OS keyring only (`gecko auth set orthogonal
--scheme bearer`). A paid RPC URL may carry its key in the query: only the host is
ever written to a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation

from gecko.livingfile import dataset
from gecko.livingfile.deps import (
    XSTOCK_PER_RUN_CAP_CENTS,
    XSTOCK_TOTAL_CAP_CENTS,
    xstock_deps_for,
)
from gecko.livingfile.job import plan_file, run_file
from gecko.livingfile.xstocks import DEFAULT_GAP_THRESHOLD_PCT, NVDAX
from gecko.modes import coerce_mode
from gecko.orthogonal.errors import ListJobError


def _threshold(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("a number of percent, like 2") from None
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError("a non-negative number of percent")
    return value


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    p.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    p.add_argument(
        "--mint", default=NVDAX.mint, help="The xStock mint (NVDAx default)."
    )
    p.add_argument("--yes", action="store_true", help="Read and pay (live).")
    p.add_argument("--rpc-url", default=None, help="Solana RPC (https).")
    p.add_argument("--jupiter-url", default=None, help="With a {mint} placeholder.")
    p.add_argument(
        "--gap-threshold-pct", type=_threshold, default=DEFAULT_GAP_THRESHOLD_PCT
    )
    p.add_argument("--per-run-cap-cents", type=int, default=XSTOCK_PER_RUN_CAP_CENTS)
    p.add_argument("--total-cap-cents", type=int, default=XSTOCK_TOTAL_CAP_CENTS)
    p.add_argument("--show", action="store_true", help="Print the stored file.")
    p.add_argument("--changes-since", metavar="RUN_ID", default=None)
    args = p.parse_args(argv)
    try:
        deps = xstock_deps_for(
            coerce_mode(args.mode),
            rpc_url=args.rpc_url,
            jupiter_url=args.jupiter_url,
            gap_threshold_pct=args.gap_threshold_pct,
            per_run_cap=args.per_run_cap_cents,
            total_cap=args.total_cap_cents,
        )
        if args.show:
            print(json.dumps(dataset.xstock_file(deps, args.mint), indent=2))
            return 0
        if args.changes_since:
            changes = dataset.xstock_changes_since(deps, args.changes_since)
            print(json.dumps(changes, indent=2))
            return 0
        plan = plan_file(deps, args.mint)
        print(f"[{deps.mode}] {plan.message}")
        for what, where, cents in plan.calls:
            print(f"  {what}: {where}  ${cents / 100:.2f}")
        print(f"balance: {plan.balance}")
        if not args.yes and deps.mode == "live":
            print("Plan only. Add --yes to read and pay.")
            return 0
        result = run_file(deps, args.mint, confirm=True)
    except ListJobError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    for row in result.rows:
        flag = f" [{row['check']}]" if row["check"] else ""
        moved = " (changed)" if row["changed"] else ""
        print(f"  {row['status']:8} {row['model_summary']}{flag}{moved}")
    for notice in result.notices:
        print(f"note: {notice}")
    print(f"charged ${result.receipt['charged_cents'] / 100:.2f}; run {result.run_id}")
    print(f"table: {result.table_path}")
    print(f"parquet: {result.parquet_path}")
    print(f"receipt: {result.receipt_path}")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
