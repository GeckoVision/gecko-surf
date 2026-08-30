"""Seed the confusable showcase onto a LIVE store — simulate by default, --broadcast to send.

    # what Claude runs, and all it may run: simulate every add against real chain state
    uv run python scripts/seed_store_showcase.py

    # what the FOUNDER runs, and only the founder
    uv run python scripts/seed_store_showcase.py --broadcast

ADD-ONLY BY CONSTRUCTION. This script builds exactly one instruction — `add_product` — and
has no code path that deletes anything. That is not a convention, it is the safety
property: `let_me_buy` has no edit instruction, so a plan that deletes first can strand a
live store half-updated with no single-instruction recovery. Here the four existing
products and their purchase history are never touched, and a failure costs one product.

WHAT IS ALREADY PROVEN, and what this still checks. The sequence ran start to finish on a
fork taken from mainnet: 16/16 landed, 515,265 CU, 80,000 lamports, read-back exactly the
intended twenty (`scripts/rehearse_store_showcase.py`). That proves the SEQUENCE. This
script re-checks each call against real chain state before it signs, because a fork is a
copy of a moment and mainnet has moved since.

THE FOUNDER BROADCASTS. Per CLAUDE.md, Claude simulates and hands over the command; it
never signs or broadcasts a mainnet transaction. Without `--broadcast` this script cannot
send: it prepares, simulates, reports, and stops. Nothing here reads the key in that mode.

STOPS ON THE FIRST FAILURE. A refused simulation ends the run rather than skipping ahead,
so the store never receives a partial menu chosen by whichever calls happened to work.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.mainnet_ledger import LedgerRow
from gecko.mainnet_ledger import record as record_ledger
from gecko.orquestra_build import orquestra_seams  # noqa: E402
from gecko.prepare_instruction import prepare_instruction_result  # noqa: E402
from gecko.rpc import RpcError, default_rpc_call  # noqa: E402
from gecko.sandbox.rehearse import _sign  # noqa: E402
from gecko.showcase import MAX_PRODUCTS, end_state, to_add  # noqa: E402
from gecko.store_accounts import receipts_pda  # noqa: E402
from gecko.store_directory import decode_store  # noqa: E402

LMB = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
DEFAULT_KEYPAIR = Path.home() / ".gecko" / "wallets" / "gecko-dev.json"


class LocalSigner:
    """The founder's own keypair, adapted to the signer seam `_sign` already expects.

    `__slots__` and a redacting `__repr__` for the same reason the ephemeral signer has
    them: a key that cannot be printed by accident cannot be leaked by a stack trace.
    """

    __slots__ = ("_keypair", "_pubkey")

    def __init__(self, path: Path) -> None:
        from solders.keypair import Keypair

        self._keypair = Keypair.from_bytes(bytes(json.loads(path.read_text())))
        self._pubkey = str(self._keypair.pubkey())

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign(self, message_bytes: bytes) -> bytes:
        return bytes(self._keypair.sign_message(message_bytes))

    def __repr__(self) -> str:
        return f"LocalSigner(pubkey={self._pubkey}, secret=<redacted>)"


def _read_store(url: str, address: str, *, min_slot: int | None = None):  # noqa: ANN202
    """Read the store, refusing a node that has not caught up to `min_slot`.

    A read-back after a broadcast is the one read that MUST NOT be served from a lagging
    node: a stale answer looks exactly like a sequence that did not land, and would report
    a successful seed as a failure. `minContextSlot` makes the node say "I am behind"
    instead of answering from an older state — a loud failure in place of a silent one.
    Observed for real: a dry run moments after the mainnet seed still showed the pre-seed
    four products from a stale public node.
    """
    config: dict[str, object] = {"encoding": "base64"}
    if min_slot is not None:
        config |= {"commitment": "confirmed", "minContextSlot": min_slot}
    for attempt in range(12):
        try:
            value = (
                default_rpc_call(url, "getAccountInfo", [address, config]).get("result")
                or {}
            ).get("value") or {}
        except (RpcError, OSError) as exc:
            # Two different failures, one correct response: the node is behind the slot we
            # require (RpcError), or the public endpoint timed out (URLError, an OSError —
            # observed against api.mainnet-beta). Both are worth waiting out; neither is
            # worth answering from.
            if attempt == 11:
                raise SystemExit(f"could not read {address} on {url}: {exc}") from exc
            time.sleep(2)
            continue
        if value:
            return decode_store(base64.b64decode(value["data"][0]), address=address)
        time.sleep(2)
    raise SystemExit(f"no store account at {address} on {url} after 12 attempts")


def _confirm(url: str, signature: str, *, seconds: int = 60) -> dict | None:
    """Poll until the transaction is visible, then hand back its meta AND its slot.

    The slot is what lets the final read-back refuse a node that has not seen this
    transaction yet, so it is carried alongside the meta rather than discarded.
    """
    for _ in range(seconds):
        result = default_rpc_call(
            url,
            "getTransaction",
            [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        ).get("result")
        if result:
            return dict(result.get("meta") or {}) | {"_slot": result.get("slot")}
        time.sleep(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the showcase onto a live store.")
    ap.add_argument(
        "--rpc-url",
        default=os.environ.get("SOLANA_RPC_URL")
        or "https://api.mainnet-beta.solana.com",
    )
    ap.add_argument("--store", default="geckocoffee")
    ap.add_argument("--keypair", type=Path, default=DEFAULT_KEYPAIR)
    ap.add_argument(
        "--broadcast",
        action="store_true",
        help="SIGN AND SEND. Founder-run only; without it nothing is signed.",
    )
    args = ap.parse_args()

    address = receipts_pda(args.store)
    store = _read_store(args.rpc_url, address)
    live = tuple((p.name, p.price_raw) for p in store.products)
    mode = "BROADCAST" if args.broadcast else "simulate only"
    print(f"store {args.store} @ {address}")
    print(f"  rpc  {args.rpc_url}")
    print(f"  mode {mode}")
    print(f"  now  {len(live)} products, {store.total_purchases} purchases")

    adds = to_add(tuple(name for name, _ in live))
    intended = end_state(live)  # raises before any signing if the plan exceeds the cap
    print(
        f"  plan {len(adds)} adds, 0 deletes -> {len(intended)}/{MAX_PRODUCTS} slots\n"
    )
    if not adds:
        print("nothing to add; the store already carries the showcase.")
        return 0

    signer = None
    if args.broadcast:
        if not args.keypair.exists():
            raise SystemExit(f"no keypair at {args.keypair}")
        signer = LocalSigner(args.keypair)
        # Checked BEFORE any fee is paid: add_product enforces the store authority, so a
        # mismatch here means sixteen guaranteed reverts, each costing a real fee.
        if signer.pubkey != store.authority:
            raise SystemExit(
                f"{args.keypair} is {signer.pubkey}, but the store's authority is "
                f"{store.authority} — every add would revert with InvalidAuthority (6004)"
            )
        print(f"  signing as {signer.pubkey} (matches the store authority)\n")

    idl_fetch, build_call = orquestra_seams()
    mint = store.products[0].mint if store.products else None
    if mint is None:
        raise SystemExit("the store carries no product to read a pricing mint from")

    total_cu = total_fee = 0
    last_slot: int | None = None
    for index, item in enumerate(adds, 1):
        label = (
            f"  {index:>2}/{len(adds)} {item.name:26} {item.price_lamports / 1e6:>6.3f}"
        )
        prepared = prepare_instruction_result(
            {
                "program_id": LMB,
                "instruction": "add_product",
                "payer": store.authority,
                "values": {
                    "store_name": args.store,
                    "name": item.name,
                    "price": item.price_lamports,
                    "mint": mint,
                    "authority": store.authority,
                },
            },
            idl_fetch=idl_fetch,
            build_call=build_call,
            rpc_call=default_rpc_call,
            rpc_url=args.rpc_url,
        )
        if prepared.get("refused"):
            print(f"{label}  REFUSED  {prepared.get('code')}: {prepared.get('reason')}")
            if prepared.get("error"):
                print(f"       error: {json.dumps(prepared['error'])}")
            for line in (prepared.get("logs") or [])[-3:]:
                print(f"       log:   {line[:110]}")
            print(
                "\nSTOPPED at the first failure rather than skipping ahead. "
                f"{index - 1} of {len(adds)} landed; the store is add-only, so what is "
                "there is correct and the rest can be re-run once this is understood."
            )
            return 2

        simulated_cu = (prepared.get("simulation") or {}).get("compute_units") or 0
        if not args.broadcast:
            print(f"{label}  simulates clean  {simulated_cu:>7,} CU")
            continue

        assert signer is not None
        signed, signature = _sign(prepared["transaction_base64"], signer)
        default_rpc_call(
            args.rpc_url,
            "sendTransaction",
            [signed, {"encoding": "base64", "skipPreflight": False}],
        )
        # Recorded at the moment of BROADCAST, before confirmation is known — a
        # transaction that lands after this script gives up is still one we sent, and a
        # ledger that only hears about confirmed ones under-counts exactly the rows
        # somebody would otherwise have to remember by hand.
        written = record_ledger(
            LedgerRow(
                signature=str(signature),
                predicted_cu=None,
                predicted_source="scripts/seed_store_showcase.py (no receipt before signing)",
                network="mainnet" if "mainnet" in args.rpc_url else "unknown",
                program="let_me_buy",
            )
        )
        if written is not None:
            print(f"{label}  logged {written}")
        meta = _confirm(args.rpc_url, signature)
        if meta is None:
            print(f"{label}  SENT, NOT CONFIRMED  {signature}")
            print(
                "\nSTOPPED. Check the signature before re-running — a confirmed add "
                "that this script did not see would revert on retry (6001)."
            )
            return 2
        if meta.get("err") is not None:
            print(f"{label}  REVERTED  {json.dumps(meta['err'])}  {signature}")
            return 2
        last_slot = meta.get("_slot") or last_slot
        cu = int(meta.get("computeUnitsConsumed") or 0)
        fee = int(meta.get("fee") or 0)
        total_cu += cu
        total_fee += fee
        print(f"{label}  {cu:>7,} CU  {fee:>6,} lamports  {signature}")

    if not args.broadcast:
        print(
            f"\nAll {len(adds)} simulate clean against live state. Nothing was signed.\n"
            "Note what this does and does not prove: each call is simulated against the "
            f"store AS IT IS NOW ({len(live)} products), so this shows every call is "
            "well-formed and authorised — the fork run is what proved they land in "
            "sequence.\n\nTo execute, the FOUNDER runs:\n"
            f"    uv run python scripts/seed_store_showcase.py --broadcast"
        )
        return 0

    after = _read_store(args.rpc_url, address, min_slot=last_slot)
    got = tuple((p.name, p.price_raw) for p in after.products)
    print(f"\nafter: {len(got)} products · {total_cu:,} CU · {total_fee:,} lamports")
    want, have = dict(intended), dict(got)
    divergences = [n for n, p in want.items() if have.get(n) != p]
    if divergences:
        print(f"DIVERGENCE: {sorted(divergences)}")
        return 1
    print(
        "read-back matches the intended end state exactly — every name and every price."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
