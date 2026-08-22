"""RUN the geckocoffee catalogue update on a fresh fork — the rehearsal, executed.

    surfpool start --no-tui --no-deploy --rpc-url <mainnet-rpc> --port 8900
    uv run python scripts/rehearse_store_update_run.py --rpc-url http://127.0.0.1:8900

`rehearse_store_update.py` computes the plan and the byte arithmetic. This one does the
thing that arithmetic cannot: lands all 35 transactions and reads the store back, so the
founder signs a sequence that has already run once, start to finish, somewhere it costs
nothing.

FORK ONLY, AND THE REAL KEY IS NEVER LOADED. `delete_product` enforces the store
authority — measured: a stranger's key gets `simulation-reverted`, the real authority
prepares clean — so the sequence needs a signature the merchant would give. Rather than
hold the merchant's key, the store is made to name a key we DO hold, through surfpool's
`surfnet_setAccount`. That is a local validator state edit, not a transaction: no
signature, no broadcast, and no counterpart on any real chain, where the field can only
be changed by the program and only for its current holder. The authority is put back at
the end.

WHAT IT PROVES: every step lands (or which one did not, and why), the real CU total, and
whether the store afterwards is EXACTLY the catalogue — names and prices, compared item
by item. A divergence of one product or one price is a failure here, which is the entire
point of doing it here.

WHAT IT ALREADY CAUGHT, 2026-08-21. The plan does not fit, and the sibling script's byte
arithmetic could not see it. `add_product` refuses the 21st product with
`VectorLimitReached` (6008): the ceiling is a COUNT OF 20, not the remaining bytes. Two
runs establish it independently — the live-shaped store (21 purchases) stopped at 20, and
a virgin store initialized on the fork with ZERO purchases and all 3,681 bytes free
stopped at 20 as well. So `rehearse_store_update.py` reporting "fits, with 306 bytes to
spare" was measuring a constraint the program does not enforce, and the catalogue's 31
items cannot live on one store at any price or in any order. Do not re-derive the plan
from byte margin; the cap is 20.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.orquestra_build import orquestra_seams  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.sandbox.deliver import _take_the_store  # noqa: E402
from gecko.sandbox.rehearse_instruction import rehearse_instruction  # noqa: E402
from gecko.sandbox.surfnet import ephemeral_signer, prove_surfnet  # noqa: E402
from gecko.semantic_catalogue import CATALOGUE  # noqa: E402
from gecko.semantic_seed import USDC_MINT  # noqa: E402
from gecko.store_accounts import receipts_pda  # noqa: E402
from gecko.store_directory import decode_store  # noqa: E402

LMB = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"


def _read(url: str, address: str):  # noqa: ANN202
    import base64

    value = (
        default_rpc_call(url, "getAccountInfo", [address, {"encoding": "base64"}])
        .get("result", {})
        .get("value")
    )
    if not value:
        raise SystemExit(f"no store at {address} on {url}")
    return decode_store(base64.b64decode(value["data"][0]), address=address)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the store update on a fork.")
    ap.add_argument("--rpc-url", default="http://127.0.0.1:8900")
    ap.add_argument("--store", default="geckocoffee")
    ap.add_argument(
        "--limit", type=int, default=0, help="stop after N steps (a smoke run)"
    )
    args = ap.parse_args()

    address = receipts_pda(args.store)
    before = _read(args.rpc_url, address)
    live = [p.name for p in before.products]
    print(f"store {args.store} @ {address}")
    print(f"  before: {len(live)} products {live}, {before.total_purchases} purchases")

    proof = prove_surfnet(args.rpc_url)
    signer = ephemeral_signer(proof)
    print(f"  fork proven; ephemeral authority {signer.pubkey}")

    taken = _take_the_store(
        proof, receipts=address, new_authority=signer.pubkey, call=default_rpc_call
    )
    print(f"  store authority taken (was {getattr(taken, 'previous_authority', '?')})")

    idl_fetch, build_call = orquestra_seams()
    catalogue = [(i.name, i.price_lamports) for i in CATALOGUE]
    mint = before.products[0].mint if before.products else USDC_MINT
    print(f"  pricing mint: {mint}")
    steps: list[tuple[str, dict]] = [
        ("delete_product", {"store_name": args.store, "product_name": n}) for n in live
    ] + [
        # The mint is a caller-supplied account on add_product — the catalogue states no
        # opinion about mints on purpose ("assigned by the store at seeding time"), and
        # this store prices in the USDC its existing products already carry. Read from the
        # store rather than hardcoded, so the rehearsal cannot price the new menu in an
        # asset the old one did not use.
        ("add_product", {"store_name": args.store, "name": n, "price": p, "mint": mint})
        for n, p in catalogue
    ]
    if args.limit:
        steps = steps[: args.limit]

    print(
        f"\nrunning {len(steps)} steps ({len(live)} deletes, {len(steps) - len(live)} adds)"
    )
    total_cu = 0
    failures: list[str] = []
    for index, (instruction, values) in enumerate(steps, 1):
        result = rehearse_instruction(
            proof,
            signer=signer,
            program_id=LMB,
            instruction=instruction,
            values={**values, "authority": signer.pubkey},
            idl_fetch=idl_fetch,
            build_call=build_call,
        )
        landed = bool(getattr(result, "landed", False))
        cu = getattr(result, "compute_units", 0) or 0
        total_cu += cu
        label = values.get("product_name") or values.get("name")
        if landed:
            print(
                f"  {index:>2}/{len(steps)} {instruction:<15} {str(label)[:24]:26} {cu:>7,} CU"
            )
        else:
            why = "; ".join(str(r) for r in (getattr(result, "refusals", ()) or ()))
            print(
                f"  {index:>2}/{len(steps)} {instruction:<15} {str(label)[:24]:26} FAILED  {why[:90]}"
            )
            failures.append(f"{instruction}({label}): {why[:120]}")
        time.sleep(0.15)

    after = _read(args.rpc_url, address)
    got = {p.name: p.price_raw for p in after.products}
    want = dict(catalogue)
    print(
        f"\nafter: {len(got)} products, {after.total_purchases} purchases, {total_cu:,} CU total"
    )

    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    wrong = sorted(n for n in set(want) & set(got) if got[n] != want[n])
    if not (missing or extra or wrong or failures):
        print("\nREAD-BACK == CATALOGUE EXACTLY (names and prices).")
    else:
        print("\nDIVERGENCE — this is what the rehearsal is for:")
        for n in missing:
            print(f"   missing   {n}")
        for n in extra:
            print(f"   extra     {n}")
        for n in wrong:
            print(f"   price     {n}: got {got[n]:,} want {want[n]:,}")
        for f in failures:
            print(f"   failed    {f}")

    # put the merchant's authority back, whatever happened above
    _take_the_store(
        proof, receipts=address, new_authority=before.authority, call=default_rpc_call
    )
    print(f"\nauthority restored to {before.authority}")
    print("Nothing touched mainnet; no merchant key was loaded.")
    return 1 if (missing or extra or wrong or failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
