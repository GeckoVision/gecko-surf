"""Prepare a REAL mainnet purchase — everything up to the signature, and nothing past it.

This produces the complete pre-flight for one `make_purchase` call and stops. It never
signs, never broadcasts, and holds no key: the founder signs, the founder sends. What it
hands back is a Receipt bound to the exact message, so the transaction that gets signed is
provably the one that was verified.

    uv run python scripts/prepare_purchase.py --signer <YOUR_PUBKEY>

What it does, in order:

1. Derives your USDC associated token account and reads its balance, so an underfunded
   run is caught here rather than on chain.
2. Asks the builder for the instruction with YOUR accounts.
3. Fetches a real blockhash and a real priority fee — the two things a simulation with a
   replaced blockhash never needed and a real send cannot do without.
4. Simulates against LIVE mainnet, read-only, with `replaceRecentBlockhash: false`, so the
   Receipt earns an **exact** binding rather than a structural one.
5. Prints the receipt, the binding, the deadline, and the exact bytes to sign.

The blockhash expires in roughly 150 slots — about a minute. That is deliberate: an exact
binding that outlived its blockhash would be attesting a message that can no longer land.
If you take longer, re-run it. Re-running is free.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.landing import (  # noqa: E402
    latest_blockhash,
    priority_fee_microlamports,
)
from gecko.rpc import default_rpc_call, user_agent  # noqa: E402
from gecko.simulate import simulate  # noqa: E402
from gecko.txbind import evaluate_tx  # noqa: E402

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

BUILD_URL = "https://api.orquestra.dev/api/p7o7nf4pucllzadrmiqhf/instructions/make_purchase/build"
#: From the published demo — the store's own accounts, unchanged. Only the buyer is ours.
STORE_RECEIPTS = "H7BjEBtan8h1HXeM38fHNPN7WxQswDhF8PFwnTuQDt5V"
STORE_AUTHORITY = "8D8qFHBnvS6oMsJy7EmGTrpoZcGd3aCC3pnPLi93Ag2V"
STORE_TOKEN_ACCOUNT = "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY"


def _rpc(url: str, method: str, params: list) -> dict:
    if method == "getAccountInfo":
        params = list(params)
        opts = (
            dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        )
        opts["encoding"] = "base64"
        params = [params[0], opts]
    return default_rpc_call(url, method, params)


def derive_ata(owner: str, mint: str) -> str:
    """The associated token account — a PDA, so it is derived, never guessed."""
    from solders.pubkey import Pubkey

    address, _bump = Pubkey.find_program_address(
        [
            bytes(Pubkey.from_string(owner)),
            bytes(Pubkey.from_string(TOKEN_PROGRAM)),
            bytes(Pubkey.from_string(mint)),
        ],
        Pubkey.from_string(ATA_PROGRAM),
    )
    return str(address)


def token_balance(account: str, rpc_url: str) -> float:
    try:
        response = default_rpc_call(rpc_url, "getTokenAccountBalance", [account])
        return float(
            (response.get("result") or {}).get("value", {}).get("uiAmount") or 0
        )
    except Exception:  # noqa: BLE001 - an unfunded/absent account reads as zero
        return 0.0


def build_instruction(
    signer: str, sender_token_account: str, product: str, table: int
) -> dict:
    body = json.dumps(
        {
            "accounts": {
                "receipts": STORE_RECEIPTS,
                "signer": signer,
                "authority": STORE_AUTHORITY,
                "mint": USDC,
                "sender_token_account": sender_token_account,
                "recipient_token_account": STORE_TOKEN_ACCOUNT,
                "token_program": TOKEN_PROGRAM,
                "system_program": SYSTEM_PROGRAM,
                "associated_token_program": ATA_PROGRAM,
            },
            "args": {
                "store_name": "jonasbar",
                "product_name": product,
                "table_number": table,
            },
            "feePayer": signer,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        BUILD_URL,
        data=body,
        headers={"content-type": "application/json", "user-agent": user_agent()},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signer", required=True, help="YOUR wallet pubkey (public only)."
    )
    parser.add_argument("--rpc-url", required=True, help="A mainnet RPC URL.")
    parser.add_argument("--product", default="Water")
    parser.add_argument("--table", type=int, default=11)
    parser.add_argument(
        "--max-usdc",
        type=float,
        default=1.0,
        help="Refuse if the account holds more than this — a spend ceiling, checked here.",
    )
    args = parser.parse_args(argv)

    ata = derive_ata(args.signer, USDC)
    balance = token_balance(ata, args.rpc_url)
    print(f"signer          {args.signer}")
    print(f"USDC account    {ata}")
    print(f"balance         {balance} USDC")
    if balance <= 0:
        print(
            "\nSTOP: that account holds no USDC — fund it before preparing a purchase."
        )
        return 2
    if balance > args.max_usdc:
        print(
            f"\nSTOP: balance {balance} exceeds the --max-usdc ceiling of {args.max_usdc}."
            "\nUse a wallet holding only what you are willing to spend: the ceiling is the"
            "\npoint of the exercise, and a receipt is not a spend limit."
        )
        return 2

    built = build_instruction(args.signer, ata, args.product, args.table)
    serialized = built.get("serializedTransaction")
    if not serialized:
        print("\nSTOP: the builder returned no serializedTransaction.")
        return 2
    print(
        f"\nbuilder said   simulationError={built.get('simulationError')} "
        f"risk={built.get('riskLevel')} wire={built.get('wireFormat')}"
    )

    blockhash, valid_until = latest_blockhash(args.rpc_url, _rpc)
    fee = priority_fee_microlamports(
        [args.signer, ata], rpc_url=args.rpc_url, rpc_call=_rpc
    )
    print(f"blockhash      {blockhash}  (valid to block height {valid_until})")
    print(f"priority fee   {fee} microlamports")

    # The builder's transaction already carries its own blockhash. We simulate exactly
    # what it produced — re-assembling here would verify a DIFFERENT transaction than the
    # one about to be signed, which is the whole failure this is meant to prevent.
    from gecko.simulate import BuiltTx

    receipt = simulate(
        {},
        rpc_url=args.rpc_url,
        rpc_call=_rpc,
        build_call=lambda _plan: BuiltTx(
            tx=serialized, encoding=built.get("encoding", "base58")
        ),
        replace_blockhash=False,
        network_label="simulated against LIVE mainnet (read-only, unsigned)",
        track=[args.signer],
    )

    print("\n" + "=" * 62)
    print(f"  RECEIPT   {receipt.status.upper()}", end="")
    if receipt.units_consumed:
        print(f"   {receipt.units_consumed:,} compute units")
    else:
        print()
    if receipt.revert_class:
        print(f"  class     {receipt.revert_class}")
    print(f"  binding   {receipt.message_binding} [{receipt.binding_strength}]")
    print(f"  network   {receipt.network_label}")
    print("=" * 62)

    for line in receipt.logs_tail[-4:]:
        print(f"    {line[:96]}")

    verdict = evaluate_tx(
        serialized, receipt, encoding=built.get("encoding", "base58"), require="exact"
    )
    print(f"\n  evaluate_tx(require=exact) → {verdict.approved}: {verdict.reason}")

    if not verdict.approved:
        print("\nDO NOT SIGN. The pre-flight did not approve this transaction.")
        return 1

    print(
        "\n  APPROVED TO SIGN — by you, not by this script. Gecko holds no key.\n"
        "  The transaction below is the one the receipt attests. Sign THESE BYTES;\n"
        f"  do not rebuild. The blockhash expires at block height {valid_until}\n"
        "  (~1 minute). If you take longer, re-run this — it is free.\n"
    )
    print(
        f"  serializedTransaction ({built.get('encoding', 'base58')}):\n\n{serialized}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
