"""Sign a prepared transaction with YOUR local keypair and broadcast it.

This is the only file in the repository that can sign a mainnet transaction, and it lives
in ``scripts/`` on purpose: nothing in ``gecko/`` signs, holds a key, or broadcasts, and
that stays true. The engine plans and verifies. This is the founder's hand on the pen.

It refuses to sign anything it has not just re-verified:

1. Decodes the transaction and checks the fee payer IS your keypair's pubkey. Signing for
   an account you do not control produces a useless signature and wastes a blockhash.
2. Re-simulates against the network you named, read-only, ``replaceRecentBlockhash: false``.
3. Requires an ``exact`` binding between that fresh receipt and these exact bytes.
4. Requires the fresh binding to equal ``--expect-binding`` — the binding
   ``prepare_purchase.py`` printed, recorded BEFORE these bytes travelled between two
   terminals.
5. Only then signs, and only then sends.

WHICH STEP CAN ACTUALLY FAIL, said plainly. Step 2 is not ceremony: a receipt is true for
the state it was taken against, so the one printed a minute ago is evidence about a
slightly older chain, and re-taking it here catches a transaction that has gone stale,
reverts against current state, or was never valid. What step 3 CANNOT catch is a swap:
the fresh receipt is computed FROM the bytes in ``--tx``, so asking whether it attests
them is asking whether a value equals itself, and a transaction with the recipient
rewritten simulates perfectly and passes. Step 4 is the only comparison in this file
whose two sides have different origins — one came from the run that was approved, the
other from the bytes in front of us now — and therefore the only one that can refuse a
substitution. Steps 2 and 3 stay because they answer questions step 4 does not.

    # look, do not send (the default)
    uv run python scripts/sign_and_send.py --network mainnet --rpc-url <url> \
        --expect-binding <sha256 from prepare_purchase> --tx <base58>

    # actually broadcast
    uv run python scripts/sign_and_send.py --network mainnet --rpc-url <url> \
        --expect-binding <sha256 from prepare_purchase> --tx <base58> --send

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

from gecko.handoff import verify_handoff  # noqa: E402
from gecko.networks import NETWORKS, coerce_network  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.txbind import _b58decode  # noqa: E402

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
    parser.add_argument(
        "--expect-binding",
        required=True,
        help=(
            "The `binding` sha256 that prepare_purchase.py printed for THESE bytes. It "
            "is the only value in this run that did not come from --tx, so it is the "
            "only one a swapped transaction cannot also satisfy. Required with no "
            "default: a value this script could supply to itself would compare against "
            "itself, which is the check this flag exists to replace."
        ),
    )
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
    # `choices` already closed the set; this narrows the type for the two call sites
    # below without a cast, and stays fail-closed if the flag is ever widened.
    network = coerce_network(args.network)

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
        network_label=f"re-simulated at signing time ({network}, read-only)",
        network=network,
        track=[signer],
    )
    print(f"\n  RECEIPT  {receipt.status.upper()}", end="")
    print(f"   {receipt.units_consumed:,} CU" if receipt.units_consumed else "")
    if receipt.revert_class:
        print(f"  class    {receipt.revert_class}")

    # IDENTITY, and the honest limit of it. `verify_handoff` asks whether this receipt
    # attests these bytes and hands back the SUBJECT re-encoded, so what gets signed below
    # is what was checked here rather than something re-derived. But the receipt one line
    # up was computed FROM `args.tx`, so the digest halves cannot differ: this refuses a
    # stale blockhash, a reverting simulation, a lookup-table message and a receipt from
    # another network — never a substitution. The substitution is caught next.
    #
    # The SAME network flag on both sides, and correct rather than tautological: this
    # script broadcasts to THIS `--rpc-url`, so "where the simulation ran" and "where the
    # signature is headed" are one fact, stated once, by the operator. It does not check
    # that the operator was right — say `mainnet` while pointed at a fork and this script
    # believes you. What it refuses is a receipt that named no network at all.
    handoff = verify_handoff(
        args.tx, receipt, encoding="base58", require="exact", expected_network=network
    )
    print(f"  binding  {handoff.approved}: {handoff.reason}")
    if not handoff.approved or handoff.transaction_base64 is None:
        print("\nDO NOT SIGN. The pre-flight did not approve these bytes.")
        return 1

    # CONTINUITY — the one comparison here that can fail. `--expect-binding` was written
    # down by a different run, in another terminal, before these bytes travelled; the
    # binding beside it was just computed from the bytes that arrived. Two origins, so
    # they can disagree, and they do exactly when something rewrote the transaction on
    # the way over — a swapped recipient, a re-quoted route, an extra instruction.
    #
    # Case and surrounding whitespace are copy-paste noise and are normalised away. A
    # changed DIGIT is not noise and is never repaired: a gate that fixes up the value it
    # is checking against is back to approving on nothing.
    expected_binding = args.expect_binding.strip().lower()
    if handoff.binding != expected_binding:
        print(
            "\nDO NOT SIGN. These are NOT the bytes that were prepared and approved."
            f"\n  expected  {expected_binding}"
            f"\n  these     {handoff.binding}"
            "\nThe simulation passed and the receipt binds these bytes — both of those"
            "\nare true of an attacker's transaction too. What does not match is the"
            "\nbinding recorded before this transaction travelled. Re-run"
            "\nprepare_purchase.py and carry ITS binding, or find out what rewrote this."
        )
        return 1
    print(f"  carried  matches --expect-binding {expected_binding}")

    if not args.send:
        print("\n  Verified and NOT sent. Re-run with --send to broadcast.")
        return 0

    # Sign the bytes the gate returned, never the ones parsed at the top: `verify_handoff`
    # hands back the SUBJECT it verified, and re-deriving the message from anything else
    # re-opens the window from the other side.
    verified = Transaction.from_bytes(base64.b64decode(handoff.transaction_base64))
    message = verified.message
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
