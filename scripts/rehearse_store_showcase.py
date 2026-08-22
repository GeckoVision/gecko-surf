"""RUN the add-only showcase sequence on a fresh fork — 16 adds, no deletes.

    surfpool start --no-tui --no-deploy --rpc-url <mainnet-rpc> --port 8912 --ws-port 8913
    uv run python scripts/rehearse_store_showcase.py --rpc-url http://127.0.0.1:8912

WHY ADD-ONLY. `scripts/rehearse_store_update.py` proposed replacing the menu, and the fork
run refused its 21st product with `VectorLimitReached` (6008) — the cap is a COUNT of 20,
not the byte margin that script had computed. The deletes land first, so on mainnet that
sequence would have stranded a live store with 21 real purchases half-updated, with no
edit instruction to recover with. This one never deletes: the four live products and their
history are carried through untouched, and a revert costs one product rather than the menu.

WHAT IT MEASURES, and it is measured rather than estimated: every add's compute units AND
its fee, read from the transaction's own `meta` after it confirms. The read-back is
compared to `gecko.showcase.end_state` item by item, names and prices both.

FORK ONLY, AND THE MERCHANT KEY IS NEVER LOADED. `add_product` enforces the store
authority, so the sequence needs a signature the merchant would give. Rather than hold
their key, the store is made to name a key we DO hold through surfpool's
`surfnet_setAccount` — a local validator state edit, not a transaction: no signature, no
broadcast, and no counterpart on any real chain, where that field can only be changed by
the program and only for its current holder. The authority is put back at the end.
"""

from __future__ import annotations

import argparse
import base64
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.orquestra_build import orquestra_seams  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.sandbox.deliver import _take_the_store  # noqa: E402
from gecko.sandbox.rehearse_instruction import rehearse_instruction  # noqa: E402
from gecko.sandbox.surfnet import ephemeral_signer, prove_surfnet  # noqa: E402
from gecko.semantic_seed import USDC_MINT  # noqa: E402
from gecko.showcase import MAX_PRODUCTS, end_state, to_add  # noqa: E402
from gecko.store_accounts import receipts_pda  # noqa: E402
from gecko.store_directory import decode_store  # noqa: E402

LMB = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"


def _read(url: str, address: str):  # noqa: ANN202
    value = (
        default_rpc_call(url, "getAccountInfo", [address, {"encoding": "base64"}]).get(
            "result"
        )
        or {}
    ).get("value") or {}
    if not value:
        raise SystemExit(f"no account at {address} on {url}")
    return decode_store(base64.b64decode(value["data"][0]), address=address)


def _fee(url: str, signature: str) -> int:
    """The fee this transaction actually paid, from its own meta — never a rate card."""
    result = default_rpc_call(
        url,
        "getTransaction",
        [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    ).get("result")
    return int(((result or {}).get("meta") or {}).get("fee") or 0)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rehearse the add-only showcase on a fork."
    )
    ap.add_argument("--rpc-url", default="http://127.0.0.1:8912")
    ap.add_argument("--store", default="geckocoffee")
    args = ap.parse_args()

    address = receipts_pda(args.store)
    before = _read(args.rpc_url, address)
    live = tuple((p.name, p.price_raw) for p in before.products)
    print(f"store {args.store} @ {address}")
    print(f"  before: {len(live)} products, {before.total_purchases} purchases")
    for name, price in live:
        print(f"     {name:24} {price / 1e6:>7.3f}")

    adds = to_add(tuple(name for name, _ in live))
    intended = end_state(live)
    print(f"\n  adding {len(adds)}; end state {len(intended)} of {MAX_PRODUCTS} slots")

    proof = prove_surfnet(args.rpc_url)
    signer = ephemeral_signer(proof)
    print(f"  fork proven; ephemeral authority {signer.pubkey}")
    _take_the_store(
        proof, receipts=address, new_authority=signer.pubkey, call=default_rpc_call
    )

    idl_fetch, build_call = orquestra_seams()
    mint = before.products[0].mint if before.products else USDC_MINT
    print(f"  pricing mint: {mint}\n")

    total_cu = total_fee = 0
    failures: list[str] = []
    for index, item in enumerate(adds, 1):
        result = rehearse_instruction(
            proof,
            signer=signer,
            program_id=LMB,
            instruction="add_product",
            values={
                "store_name": args.store,
                "name": item.name,
                "price": item.price_lamports,
                "mint": mint,
                "authority": signer.pubkey,
            },
            idl_fetch=idl_fetch,
            build_call=build_call,
        )
        if not result.landed:
            why = "; ".join(str(r) for r in (result.refusals or ())) or str(
                result.error
            )
            print(f"  {index:>2}/{len(adds)} {item.name:26} FAILED  {why[:80]}")
            failures.append(f"{item.name}: {why[:120]}")
            continue
        cu = result.compute_units or 0
        fee = _fee(args.rpc_url, result.signature) if result.signature else 0
        total_cu += cu
        total_fee += fee
        print(
            f"  {index:>2}/{len(adds)} {item.name:26} "
            f"{item.price_lamports / 1e6:>6.3f}  {cu:>7,} CU  {fee:>6,} lamports"
        )

    after = _read(args.rpc_url, address)
    got = tuple((p.name, p.price_raw) for p in after.products)
    print(
        f"\nafter: {len(got)} products, {after.total_purchases} purchases · "
        f"{total_cu:,} CU · {total_fee:,} lamports"
    )

    divergences = [f"failed add {f}" for f in failures]
    want, have = dict(intended), dict(got)
    for name, price in sorted(want.items()):
        if name not in have:
            divergences.append(f"missing   {name}")
        elif have[name] != price:
            divergences.append(
                f"repriced  {name}: {have[name]} on chain, {price} intended"
            )
    for name in sorted(set(have) - set(want)):
        divergences.append(f"unexpected {name}")

    if divergences:
        print("\nDIVERGENCE — this is what the rehearsal is for:")
        for line in divergences:
            print(f"   {line}")
    else:
        print(
            f"\nread-back matches the intended end state EXACTLY — "
            f"{len(got)} products, every name and every price."
        )

    _take_the_store(
        proof, receipts=address, new_authority=before.authority, call=default_rpc_call
    )
    print(f"\nauthority restored to {before.authority}")
    print("Nothing touched mainnet; no merchant key was loaded.")
    return 1 if divergences else 0


if __name__ == "__main__":
    raise SystemExit(main())
