"""Measure token overlap between purchase intents and the live menu — never constrain it.

The existing golden sets carry a CI-enforced invariant that every `paraphrase_no_overlap`
goal shares no token with its target. That invariant is correct for what it was built to
do — it guarantees the empty-drop path is exercised — but it means a lexical arm scoring
0.00 on that archetype is arithmetic, not evidence. We read that number as a ranker
weakness for months.

This set inverts the relationship. Overlap is MEASURED and reported per row, and nothing
rejects a row for having too much or too little. A realistic set has a spread; if every
row here lands at zero we have rebuilt the old fixture by hand, and the distribution says
so before anyone quotes a recall figure off it.

Folded tokens, not raw: `gecko.lexnorm.fold_tokens` is what the live catalog applies, and
measuring with the raw tokenizer reports plurals as misses that the real path handles.
That mistake was made, on this set, before this script existed.

    uv run python scripts/purchase_intent_overlap.py            # measure against a snapshot
    uv run python scripts/purchase_intent_overlap.py --live     # re-read the menu from chain
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gecko.catalog import _tokens
from gecko.lexnorm import fold_tokens

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tests" / "fixtures" / "golden" / "purchase_intents.jsonl"
SNAPSHOT = ROOT / "tests" / "fixtures" / "golden" / "purchase_menu_snapshot.json"
MAINNET = "https://api.mainnet-beta.solana.com"


def _folded(text: str) -> set[str]:
    return set(fold_tokens(set(_tokens(text))))


def read_menu(live: bool) -> dict[str, list[str]]:
    """The menu the intents are scored against — a frozen snapshot by default.

    Live is opt-in because a menu that moves under the eval turns a ranking change into
    an unexplainable delta; the snapshot is what makes two runs comparable.
    """
    if not live:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    from gecko.store_directory import list_stores

    listing = list_stores(rpc_url=MAINNET)
    out: dict[str, list[str]] = {}
    for store in listing.get("stores", []):
        name = store.get("store")
        products = [p.get("name") for p in store.get("products", ())]
        if name and products:
            out[str(name)] = [str(p) for p in products]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="re-read the menu from mainnet")
    ap.add_argument("--write-snapshot", action="store_true")
    args = ap.parse_args(argv)

    menu = read_menu(args.live)
    if args.write_snapshot:
        SNAPSHOT.write_text(
            json.dumps(menu, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        print(
            f"wrote {SNAPSHOT.relative_to(ROOT)} — {sum(len(v) for v in menu.values())} products"
        )
        return 0

    by_product = {(s, p): _folded(p) for s, ps in menu.items() for p in ps}
    rows = [
        json.loads(line)
        for line in TASKS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    print(f"{'#':<3}{'archetype':<24}{'ovl':<5}{'plan':<16}goal")
    print("-" * 96)
    spread: list[int] = []
    for i, row in enumerate(rows, 1):
        goal_tokens = _folded(row["goal"])
        best = max((len(goal_tokens & t) for t in by_product.values()), default=0)
        if row["archetype"] != "out_of_scope":
            spread.append(best)
        print(
            f"{i:<3}{row['archetype']:<24}{best:<5}{row['expect_plan']:<16}{row['goal'][:44]}"
        )

    print()
    print(f"overlap spread (positives only, n={len(spread)}): {sorted(spread)}")
    zeros = spread.count(0)
    print(f"  rows at zero: {zeros}/{len(spread)}")
    if spread and zeros == len(spread):
        print(
            "  WARNING: every positive row is at zero — this is the old fixture rebuilt by hand."
        )
    if spread and min(spread) == max(spread):
        print("  WARNING: no variance — the set measures one case, not a distribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
