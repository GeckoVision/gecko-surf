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
5. Prints the receipt, the binding, the deadline, the exact bytes to sign, and the
   ``sign_and_send.py`` command that carries this run's binding forward.

WHAT THE PRE-FLIGHT HERE PROVES, and what it does not. The receipt passed, it covers the
whole message including the blockhash (``exact``), it names the network YOU named, and
the message loads no accounts from a lookup table — all real, all worth refusing on. What
it does NOT prove is that these bytes are the bytes anyone else has: the transaction we
simulated and the transaction we then check are one variable, so that half of the check
compares a value with itself and cannot fail. The comparison that CAN fail happens in the
next script, against the binding printed here — which is why that binding is part of the
command this run prints rather than a decoration on the receipt.

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
from gecko.networks import NETWORKS, coerce_network  # noqa: E402
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
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help=(
            "Prepare this many purchases, one at a time. Each round takes a FRESH "
            "blockhash so every binding is exact; you sign each as it appears rather "
            "than batching, because an exact binding that outlived its blockhash would "
            "attest a message that can no longer land."
        ),
    )
    parser.add_argument("--price-usdc", type=float, default=0.1)
    parser.add_argument(
        "--network",
        required=True,
        choices=sorted(NETWORKS),
        help=(
            "Which network --rpc-url points at. YOU say; nothing here guesses. A fork "
            "proxy answers at any hostname, so the URL is evidence of nothing. There is "
            "no default: 'unknown' is a legal answer and refuses at the gate."
        ),
    )
    args = parser.parse_args(argv)

    if args.count < 1:
        print("STOP: --count must be at least 1.")
        return 2
    planned = args.count * args.price_usdc
    if planned > args.max_usdc + 1e-9:
        print(
            f"STOP: {args.count} x {args.price_usdc} USDC = {planned:.2f}, over the "
            f"--max-usdc ceiling of {args.max_usdc}."
            "\nThe ceiling binds the WHOLE run, not one purchase — that is the point of it."
        )
        return 2

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

    if args.count > 1:
        print(
            f"\nplan            {args.count} purchases x {args.price_usdc} USDC "
            f"= {planned:.2f} USDC total (ceiling {args.max_usdc})"
        )

    for round_number in range(1, args.count + 1):
        if args.count > 1:
            print(f"\n{'-' * 62}\n  ROUND {round_number} of {args.count}\n{'-' * 62}")
        code = _prepare_one(args, ata)
        if code != 0:
            print(
                f"\nSTOPPED at round {round_number} of {args.count}. Nothing further was "
                "prepared.\nEarlier rounds you already signed are unaffected; this run "
                "refuses to continue\npast a round it could not approve."
            )
            return code
        if round_number < args.count:
            # Deliberately blocking. Each round's binding is exact and expires with its
            # blockhash, so preparing the next one before this one is signed would hand
            # you a queue of transactions that quietly go stale while you click.
            input(
                "\n  Sign and broadcast the bytes above, then press Enter for the next "
                "round. "
            )
    return 0


def _prepare_one(args: argparse.Namespace, ata: str) -> int:
    """One purchase: build, fetch a fresh blockhash and fee, simulate live, approve, stop.

    Everything up to the signature and nothing past it. Returns 0 when the receipt
    approved the exact bytes printed, non-zero otherwise — and a non-zero return stops
    the whole run rather than moving on to the next round.
    """
    # `choices` already closed the set on the parser; this narrows the type for the two
    # call sites below without a cast, and stays fail-closed if the flag is ever widened.
    network = coerce_network(args.network)
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
        network_label=f"simulated against {network} (read-only, unsigned)",
        network=network,
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
    print(f"  network   {receipt.network}   (as you stated it, never inferred)")
    print(f"  caveat    {receipt.network_label}")
    print("=" * 62)

    for line in receipt.logs_tail[-4:]:
        print(f"    {line[:96]}")

    # A SELF-CHECK, and it is labelled as one because half of it cannot fail. `serialized`
    # is what was simulated a few lines up AND what is passed here, so `presented` and
    # `attested` are two hashes of one string: the identity comparison is a tautology and
    # would approve an attacker's transaction just as readily, had one arrived. It is kept
    # because the REST of this call is not a tautology — it refuses a receipt that did not
    # pass, one carrying only a structural binding, one whose message loads accounts from
    # a lookup table, and one from a network the operator did not name.
    #
    # The falsifiable comparison happens in the NEXT script: the binding printed below is
    # recorded here, before these bytes travel, and `sign_and_send.py --expect-binding`
    # checks it against a binding computed from whatever bytes actually arrive. That is
    # the only place the two sides have different origins, and therefore the only place a
    # swapped recipient can be refused.
    #
    # The SAME network flag on both sides, and correct rather than tautological: these
    # bytes are going to be signed and submitted to THIS `--rpc-url`, so "where the
    # simulation ran" and "where the signature is headed" are one fact, stated once, by
    # the operator. It does not check that the operator was right — say `mainnet` while
    # pointed at a fork and this script believes you. What it does refuse is a receipt
    # from somewhere else, and one that named no network at all.
    verdict = evaluate_tx(
        serialized,
        receipt,
        encoding=built.get("encoding", "base58"),
        require="exact",
        expected_network=network,
    )
    print(
        f"\n  self-check (require=exact) → {verdict.approved}: {verdict.reason}"
        "\n  Reads: this receipt passed, on the network you named, with an exact binding"
        "\n  over a message that loads no lookup accounts. It does NOT read as 'these"
        "\n  bytes were independently verified' — both sides of the digest comparison"
        "\n  came from the same string. Carry the binding to the signing step for a"
        "\n  comparison that can fail."
    )

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
    # The handoff. `--expect-binding` is what makes the signing step's check falsifiable,
    # so it is printed as part of the command rather than left for someone to copy out of
    # the receipt block. The RPC URL is deliberately NOT echoed: it frequently carries an
    # API key, and this transcript ends up in terminals, logs and demo recordings.
    print(
        "  Next, in the terminal that holds your key:\n\n"
        "    uv run python scripts/sign_and_send.py \\\n"
        f"      --network {network} --rpc-url <the same --rpc-url you passed here> \\\n"
        f"      --expect-binding {receipt.message_binding} \\\n"
        "      --tx <the serializedTransaction above>\n\n"
        "  That run re-simulates against the chain as it is THEN, and refuses unless the\n"
        "  binding it computes from the bytes you hand it equals the one above.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
