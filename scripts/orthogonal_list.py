"""Who commented on an Instagram post, and who already buys? Thin CLI over gecko.orthogonal.

    # $0, made-up data (writes a made-up sample sheet if asked):
    uv run python scripts/orthogonal_list.py --post-url https://www.instagram.com/p/<code>/ \\
        --customers clientes.csv --write-sample-sheet

    # live: prints the plan (free) and stops
    uv run python scripts/orthogonal_list.py --mode live --post-url <url> --customers clientes.csv
    # live: plan, then pay (about $0.02, capped at $0.10 per run, $1.00 in total)
    uv run python scripts/orthogonal_list.py --mode live --post-url <url> --customers clientes.csv --yes

The key comes from the OS keyring only (`gecko auth set orthogonal --scheme bearer`).
"""

from __future__ import annotations

import argparse
import json
import sys

from gecko.modes import coerce_mode
from gecko.orthogonal import deps_for, plan_list, run_list
from gecko.orthogonal.errors import ListJobError
from gecko.orthogonal.fixture import SAMPLE_SHEET_CSV
from gecko.orthogonal.sheet import resolve_in_folder


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--post-url", required=True)
    p.add_argument("--customers", required=True, help="CSV file name in ~/gecko-lists/")
    p.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    p.add_argument("--yes", action="store_true", help="Pay and build the list.")
    p.add_argument(
        "--write-sample-sheet",
        action="store_true",
        help="recorded only: write the made-up sample sheet under --customers first.",
    )
    args = p.parse_args(argv)
    deps = deps_for(coerce_mode(args.mode))
    if args.write_sample_sheet:
        if deps.mode != "recorded":
            print("--write-sample-sheet is for recorded mode only.", file=sys.stderr)
            return 2
    try:
        if args.write_sample_sheet:
            deps.folder.mkdir(parents=True, exist_ok=True)
            target = resolve_in_folder(args.customers, deps.folder)
            target.write_text(SAMPLE_SHEET_CSV, encoding="utf-8")
        plan = plan_list(deps, args.post_url, args.customers)
        print(f"[{deps.mode}] {plan.message}")
        for notice in plan.notices:
            print(f"note: {notice}")
        print(
            f"balance: {plan.balance}  rehearsal left: ${plan.rehearsal_left_cents / 100:.2f}"
        )
        if not args.yes and deps.mode == "live":
            print("Plan only. Add --yes to pay and build the list.")
            return 0
        result = run_list(deps, args.post_url, args.customers, confirm=True)
    except ListJobError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2))
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
