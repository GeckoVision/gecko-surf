"""Watch suppliers: identity, parent, news, trust, SDN name check, website changes.

Thin CLI over gecko.orthogonal.supplier. Two sources per field where the plan lists two;
agreement computed in code.

    # $0, made-up companies (writes the made-up sample sheet if asked):
    uv run python scripts/supplier_watch.py --sheet suppliers.csv --write-sample-sheet

    # live: prints the plan (free) and stops
    uv run python scripts/supplier_watch.py --mode live --sheet suppliers.csv
    # live: plan, then pay (capped at $0.60 per run, $0.90 in total)
    uv run python scripts/supplier_watch.py --mode live --sheet suppliers.csv --yes

    # every stored run, back from Parquet
    uv run python scripts/supplier_watch.py --read-runs

    # $0 self-repair: two recorded runs, the second with crustdata's shape changed
    uv run python scripts/supplier_watch.py --demo-self-repair

    # live, forced failover: entity's first source is not called this run
    uv run python scripts/supplier_watch.py --mode live --sheet suppliers.csv \
        --force-failover entity --yes

In live mode, --write-sample-sheet writes the founder's demo list (Jabil, Flex,
Celestica). It never overwrites a file that is already there.
The key comes from the OS keyring only (`gecko auth set orthogonal --scheme bearer`).
"""

from __future__ import annotations

import argparse
import json
import sys

from gecko.modes import coerce_mode
from gecko.orthogonal.errors import ListJobError
from gecko.orthogonal.sheet import resolve_in_folder
from gecko.orthogonal.supplier import (
    plan_watch,
    read_runs,
    run_watch,
    supplier_deps_for,
)
from gecko.orthogonal.supplier.demo import run_self_repair_demo
from gecko.orthogonal.supplier.deps import (
    WATCH_PER_RUN_CAP_CENTS,
    WATCH_TOTAL_CAP_CENTS,
)
from gecko.orthogonal.supplier.sources import SOURCE_MAP
from gecko.orthogonal.supplier.fixture import DEMO_SUPPLIERS_CSV, SAMPLE_SUPPLIERS_CSV


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--sheet", help="CSV file name in ~/gecko-lists/")
    p.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    p.add_argument("--yes", action="store_true", help="Pay and run the watch.")
    p.add_argument("--write-sample-sheet", action="store_true")
    p.add_argument("--sdn-url", default=None, help="Override the OFAC SDN XML URL.")
    p.add_argument("--per-run-cap-cents", type=int, default=WATCH_PER_RUN_CAP_CENTS)
    p.add_argument("--total-cap-cents", type=int, default=WATCH_TOTAL_CAP_CENTS)
    p.add_argument(
        "--read-runs", action="store_true", help="Print stored runs and exit."
    )
    p.add_argument(
        "--force-failover",
        action="append",
        default=[],
        choices=sorted(SOURCE_MAP),
        help="Do not call this field's first source; fill it from the next one.",
    )
    p.add_argument("--gleif-base-url", default=None, help="Override GLEIF's API root.")
    p.add_argument(
        "--demo-self-repair",
        action="store_true",
        help="Two recorded runs, the second with a changed upstream shape ($0).",
    )
    args = p.parse_args(argv)
    if args.demo_self_repair:
        print("\n".join(run_self_repair_demo().lines()))
        return 0
    try:
        deps = supplier_deps_for(
            coerce_mode(args.mode),
            sdn_url=args.sdn_url,
            per_run_cap=args.per_run_cap_cents,
            total_cap=args.total_cap_cents,
            force_failover=args.force_failover,
            gleif_base_url=args.gleif_base_url,
        )
    except ListJobError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    try:
        if args.read_runs:
            print(json.dumps(read_runs(deps.base.folder), indent=2))
            return 0
        if not args.sheet:
            p.error("--sheet is required")
        if args.write_sample_sheet:
            deps.base.folder.mkdir(parents=True, exist_ok=True)
            target = resolve_in_folder(args.sheet, deps.base.folder)
            if target.exists():
                print(f"{target.name} already exists; using it as it is.")
            else:
                live = deps.mode == "live"
                target.write_text(
                    DEMO_SUPPLIERS_CSV if live else SAMPLE_SUPPLIERS_CSV,
                    encoding="utf-8",
                )
        plan = plan_watch(deps, args.sheet)
        print(f"[{deps.mode}] {plan.message}")
        for quote in plan.suppliers:
            print(f"  {quote.name} ({quote.domain}): ${quote.cents / 100:.2f}")
            for endpoint, cents in quote.calls:
                print(f"      {endpoint}: ${cents / 100:.2f}")
        for notice in plan.notices:
            print(f"note: {notice}")
        print(f"balance: {plan.balance}  sanctions list: {plan.sdn_url}")
        if not args.yes and deps.mode == "live":
            print("Plan only. Add --yes to pay and run the watch.")
            return 0
        result = run_watch(deps, args.sheet, confirm=True)
    except ListJobError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2))
    for line in result.repairs:
        print(f"repair: {line}")
    print(f"table: {result.table_path}")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
