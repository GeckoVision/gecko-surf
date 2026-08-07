"""Sign a prepared transaction with YOUR local keypair and broadcast it.

This is the only file in the repository that can sign a mainnet transaction, and it lives
in ``scripts/`` on purpose: nothing in ``gecko/`` signs, holds a key, or broadcasts, and
that stays true. The engine plans and verifies. This is the founder's hand on the pen.

It refuses to sign anything it has not just re-verified:

1. Decodes the transaction and checks the fee payer IS your keypair's pubkey. Signing for
   an account you do not control produces a useless signature and wastes a blockhash.
2. Re-simulates against LIVE mainnet, read-only, with ``replaceRecentBlockhash: false``.
3. Requires an ``exact`` binding between that fresh receipt and these exact bytes.
4. Only then signs, and only then sends.

Step 2 is not ceremony. A receipt is true for the state it was taken against, so the one
printed a minute ago in another terminal is evidence about a slightly older chain. This
re-takes it at the moment of signing, which is the rule we publish and therefore the rule
we follow.

    # look, do not send (the default)
    uv run python scripts/sign_and_send.py --tx <base58> --rpc-url <mainnet>

    # actually broadcast
    uv run python scripts/sign_and_send.py --tx <base58> --rpc-url <mainnet> --send

The keypair is read from ~/.config/solana/id.json unless --keypair says otherwise. It is
never printed, never logged, and never leaves this process.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.txbind import _b58decode, evaluate_tx  # noqa: E402

DEFAULT_KEYPAIR = Path.home() / ".config" / "solana" / "id.json"


def _rpc(url: str, method: str, params: list) -> dict:
    return default_rpc_call(url, method, params)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tx", required=True, help="The base58 serializedTransaction.")
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--keypair", type=Path, default=DEFAULT_KEYPAIR)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually broadcast. Without it this only verifies and shows what it would do.",
    )
    args = parser.parse_args(argv)

    from solders.keypair import Keypair
    from solders.transaction import Transaction

    if not args.keypair.exists():
        print(f"STOP: no keypair at {args.keypair}")
        return 2
    keypair = Keypair.from_bytes(bytes(json.loads(args.keypair.read_text())))
    signer = str(keypair.pubkey())

    raw = _b58decode(args.tx)
    transaction = Transaction.from_bytes(raw)
    message = transaction.message
    fee_payer = str(message.account_keys[0])

    print(f"  keypair pubkey  {signer}")
    print(f"  tx fee payer    {fee_payer}")
    if fee_payer != signer:
        print(
            "\nSTOP: this transaction pays from an account your keypair does not control."
            "\nSigning it would produce a signature the network rejects. Prepare the"
            "\ntransaction with --signer set to the pubkey above."
        )
        return 2

    # Re-take the receipt NOW, against the state we are about to commit into.
    receipt = simulate(
        {},
        rpc_url=args.rpc_url,
        rpc_call=_rpc,
        build_call=lambda _plan: BuiltTx(tx=args.tx, encoding="base58"),
        replace_blockhash=False,
        network_label="re-simulated at signing time (live mainnet, read-only)",
        track=[signer],
    )
    print(f"\n  RECEIPT  {receipt.status.upper()}", end="")
    print(f"   {receipt.units_consumed:,} CU" if receipt.units_consumed else "")
    if receipt.revert_class:
        print(f"  class    {receipt.revert_class}")

    verdict = evaluate_tx(args.tx, receipt, encoding="base58", require="exact")
    print(f"  binding  {verdict.approved}: {verdict.reason}")
    if not verdict.approved:
        print("\nDO NOT SIGN. The pre-flight did not approve these bytes.")
        return 1

    if not args.send:
        print("\n  Verified and NOT sent. Re-run with --send to broadcast.")
        return 0

    signature = keypair.sign_message(bytes(message))
    signed = Transaction.populate(message, [signature])

    # A base64 STRING, not a byte array. sendTransaction takes the encoded transaction
    # as its first param; passing a list gets "cannot unmarshal array into Go value of
    # type string" from the node, after the signature already exists locally.
    encoded = base64.b64encode(bytes(signed)).decode()
    reply = _rpc(
        args.rpc_url,
        "sendTransaction",
        [encoded, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
    )
    if "error" in reply:
        print(f"\n  send failed: {reply['error']}")
        return 1
    print(f"\n  SENT  {reply.get('result')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
