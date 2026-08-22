"""Rehearse the geckocoffee catalogue update on a FORK, before anything is signed on mainnet.

    # terminal 1 — fork mainnet at the current slot
    surfpool start --no-tui --no-deploy --rpc-url <mainnet-rpc> --port 8899
    # terminal 2
    uv run python scripts/rehearse_store_update.py --rpc-url http://127.0.0.1:8899

WHY THIS EXISTS. `add_product` sets a product's price and there is NO instruction that
edits one: the program has add_product / delete_product / delete_store / initialize /
make_purchase / mark_as_delivered / update_details / update_telegram_channel, and
`update_details` takes a STORE-level string with no product and no price. So a wrong
number costs two signed mainnet transactions to undo, and the sequence has to be right
the first time.

WHAT IT PROVES, and what it refuses to claim:

* the exact ORDER — `delete_product` on the live `Cappuccino` must land BEFORE the
  catalogue's `Cappuccino` is added, or that one add reverts with ProductAlreadyExists
  (6001) and takes nothing else with it.
* whether all 31 FIT. Every let_me_buy store on mainnet is exactly 3,681 bytes — measured
  across five stores holding 4 to 127 purchases — so the account is allocated at
  `initialize` and never reallocs. `VectorLimitReached` (6008) is what guards that. This
  computes the end-state size from the same encoder the fork seeder uses and reports the
  margin.
* what the store looks like AFTER, read back through the decode path a purchase uses.

It signs nothing on mainnet and needs no key: the plan and the arithmetic are computed
from the catalogue and one read of the live store. Where a step cannot be proven without
executing it, this says so rather than implying it passed.
"""

from __future__ import annotations

import argparse
import base64
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.semantic_catalogue import CATALOGUE  # noqa: E402
from gecko.store_accounts import receipts_pda  # noqa: E402
from gecko.semantic_catalogue import to_store_config  # noqa: E402
from gecko.semantic_seed import USDC_MINT, config_to_listing  # noqa: E402
from gecko.store_directory import decode_store, encode_store  # noqa: E402

#: Measured across geckocoffee, jonasbar, solanaspaces, superteststore and alexsbar —
#: every one, whatever its history. The program allocates at initialize and never grows.
KNOWN_ACCOUNT_SIZE = 3_681

#: MEASURED on a fork, 2026-08-21, and it is a COUNT — not a byte budget. `add_product`
#: refuses the 21st product with `VectorLimitReached` (6008) on a store with 21 purchases
#: AND on a virgin store with zero purchases and every one of its 3,681 bytes free. The
#: byte margin below is therefore necessary but NOT sufficient, and reporting it alone is
#: what let this script call a 31-item plan a pass.
MAX_PRODUCTS = 20


def plan(live_names: list[str], catalogue_names: list[str]) -> dict[str, list[str]]:
    """The delete/add plan, ordered so no step reverts.

    A collision is a DELETE THEN ADD, never an add: `add_product` on an existing name
    reverts (6001), and there is no edit instruction. A live name absent from the
    catalogue is a plain delete — the founder's end state is the store EXACTLY the
    catalogue.
    """
    live, cat = set(live_names), set(catalogue_names)
    return {
        # collisions first: delete before the re-add, or the add reverts
        "delete_collisions": sorted(live & cat),
        "delete_legacy": sorted(live - cat),
        "add": [n for n in catalogue_names if n not in (live - cat)],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Rehearse the store update on a fork.")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:8899")
    ap.add_argument("--store", default="geckocoffee")
    ap.add_argument(
        "--read-mainnet",
        action="store_true",
        help="read the CURRENT store from mainnet instead of the fork (read-only)",
    )
    args = ap.parse_args()

    url = "https://api.mainnet-beta.solana.com" if args.read_mainnet else args.rpc_url
    address = receipts_pda(args.store)
    resp = default_rpc_call(url, "getAccountInfo", [address, {"encoding": "base64"}])
    value = (resp.get("result") or {}).get("value")
    if not value:
        print(f"no store at {address} on {url}", file=sys.stderr)
        return 2

    raw = base64.b64decode(value["data"][0])
    listing = decode_store(raw, address=address)
    live_names = [p.name for p in listing.products]
    cat_names = [i.name for i in CATALOGUE]

    print(f"store           {args.store}  ({address})")
    print(f"read from       {url}")
    print(
        f"allocated       {len(raw):,} bytes"
        f"{'  <-- differs from the known 3,681!' if len(raw) != KNOWN_ACCOUNT_SIZE else ''}"
    )
    print(f"live products   {len(live_names)}: {live_names}")
    print(f"purchases       {listing.total_purchases}")

    steps = plan(live_names, cat_names)
    print(
        f"\nPLAN — {len(steps['delete_collisions'])} collision delete(s), "
        f"{len(steps['delete_legacy'])} legacy delete(s), {len(steps['add'])} add(s)"
    )
    for n in steps["delete_collisions"]:
        print(
            f"   1. delete_product {n!r}   (collides with the catalogue — must precede its add)"
        )
    for n in steps["delete_legacy"]:
        print(f"   2. delete_product {n!r}   (not in the catalogue)")
    print(f"   3. add_product x{len(steps['add'])}")

    # --- the decisive arithmetic, computed not guessed -------------------------------
    # The end state's PRODUCT bytes, through the same encoder the fork seeder uses. The
    # receipts are left exactly as they are: this update never touches them.
    # Build the end state through the SAME path the fork seeder uses, rather than
    # hand-assembling one — a second constructor is a second thing to drift.
    end_listing = config_to_listing(
        to_store_config(args.store),
        authority=listing.authority,
        address=address,
        mint=listing.products[0].mint if listing.products else USDC_MINT,
        decimals=6,
    )
    end = encode_store(end_listing)
    now = encode_store(listing)
    receipts_bytes = len(raw) - len(raw.rstrip(b"\x00"))
    content_now = len(raw) - receipts_bytes
    print("\nBYTES")
    print(f"   allocated (fixed at initialize)   {len(raw):>7,}")
    print(f"   content today                     {content_now:>7,}")
    print(f"   free today                        {receipts_bytes:>7,}")
    print(f"   products today encode to          {len(now):>7,}")
    print(f"   products after the update         {len(end):>7,}")
    growth = len(end) - len(now)
    print(f"   growth needed                     {growth:>+7,}")
    margin = receipts_bytes - growth
    print(f"   margin after                      {margin:>+7,}")
    end_count = len(end_listing.products)
    print(
        f"\n   products after the update         {end_count:>7,} of {MAX_PRODUCTS} allowed"
    )
    if end_count > MAX_PRODUCTS:
        print(
            f"\n   REFUSED: {end_count} products exceeds the program's cap of "
            f"{MAX_PRODUCTS}. add_product number {MAX_PRODUCTS + 1} reverts with "
            "VectorLimitReached (6008) and the store is left HALF-UPDATED — the deletes "
            "already landed. The byte margin above is irrelevant: the cap is a count, "
            "measured on a store with no purchases and every byte free."
        )
        return 3
    if margin < 0:
        print(
            "\n   REFUSED: the end state does not fit in the allocated bytes. Some "
            "add_product WILL hit VectorLimitReached (6008), and the store would be "
            "left half-updated."
        )
        return 3
    print(
        f"\n   fits: {end_count} of {MAX_PRODUCTS} product slots, {margin:,} bytes to spare "
        f"(~{margin // 75} more receipts at ~75 bytes each)"
    )

    print("\nNOT PROVEN HERE — run the sequence on the fork to establish:")
    print(
        "   * that each build/sign/submit actually lands (this computed the plan, not the txs)"
    )
    print("   * the real CU and fee total")
    print("   * that the read-back after step 3 is EXACTLY the catalogue")
    print("\nNothing was signed and nothing was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
