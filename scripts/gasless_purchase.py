"""One relay-paid purchase, end to end, with every rail on: the fork rehearsal runner.

    uv run python scripts/gasless_purchase.py \\
        --network fork --rpc-url http://127.0.0.1:8899 \\
        --buyer-keypair ~/.gecko/wallets/usdg-nosol-buyer.json \\
        --store geckocoffee --product Espresso [--table 1] [--max-spend-usdc 0.25] \\
        [--broadcast]

    KORA_RPC_URL=http://127.0.0.1:8080  KORA_API_KEY=...   in the environment

What it does, in order, and each step is the package's, not this file's:

  1. ``prepare_purchase`` (the MCP tool) with ``fee_payer`` = the relay's own account:
     resolved accounts, a passing receipt, UNSIGNED bytes bound at ``exact``.
  2. Without ``--broadcast`` it stops here and prints what it would have sent. The relay
     is never asked for a signature in a dry run, because a Kora signature is a real
     policy decision on the relay's side and a real simulation on its node.
  3. With ``--broadcast``: :func:`gecko.autonomous_purchase.settle_sponsored`. The relay
     signs first (and appends its Lighthouse assertion); the bytes are re-simulated and
     re-verified; the buyer signs in the ``authority`` role behind the spend gate; the two
     signatures are merged; ONE transaction is sent and confirmed.
  4. Judge by what MOVED: the buyer's SOL before and after must be equal. That line is
     the gasless claim; everything else is the purchase working.

The buyer's key is read from a file by :class:`AuthorityKeypairBackend`, which signs its
own slot and nothing else, outside ``gecko/``. The relay's key is never here at all.

Per CLAUDE.md the mainnet broadcast is founder-run: ``--network mainnet --broadcast``
spends real money on both sides and is the founder's decision to type.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.autonomous_purchase import (  # noqa: E402
    PurchaseSettled,
    default_spend_policy,
    settle_sponsored,
)
from gecko.networks import coerce_network  # noqa: E402
from gecko.prepare_purchase import prepare_purchase_result  # noqa: E402
from gecko.rpc import default_rpc_call, validate_rpc_url  # noqa: E402
from gecko.signer import (  # noqa: E402
    AUTHORITY_ROLE,
    DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
    SignerProfile,
    SigningAttestation,
    TransactionSigner,
)
from gecko.spend_policy import InMemorySpendLedger, SpendPolicyGate  # noqa: E402
from scripts.kora_relay import KoraRelay, KoraRelayError  # noqa: E402


class AuthorityKeypairBackend:
    """The buyer's key, OUTSIDE ``gecko/``. Signs ITS slot; leaves the relay's alone.

    Different from ``scripts/autonomous_purchase.py``'s ``LocalKeypairBackend``, which
    populates slot 0 because it IS the fee payer. Under a relay the buyer is slot 1, and
    ``partial_sign`` puts the signature where the message says this key belongs.
    """

    def __init__(self, path: Path) -> None:
        from solders.keypair import Keypair

        self._keypair = Keypair.from_bytes(bytes(json.loads(path.read_text())))

    @property
    def pubkey(self) -> str:
        return str(self._keypair.pubkey())

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        from solders.transaction import Transaction

        if attestation.signing_as != AUTHORITY_ROLE:
            raise RuntimeError(
                "this backend signs as the authority, never as fee payer"
            )
        transaction = Transaction.from_bytes(unsigned_transaction)
        transaction.partial_sign([self._keypair], transaction.message.recent_blockhash)
        return bytes(transaction)


def _balance(rpc_url: str, account: str) -> int:
    reply = default_rpc_call(rpc_url, "getBalance", [account])
    value = (reply.get("result") or {}).get("value")
    return int(value) if isinstance(value, int) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--network", choices=["fork", "mainnet"], required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--buyer-keypair", type=Path, required=True)
    parser.add_argument("--store", default="geckocoffee")
    parser.add_argument("--product", required=True)
    parser.add_argument("--table", type=int, default=1)
    parser.add_argument("--max-spend-usdc", type=float, default=1.0)
    parser.add_argument("--broadcast", action="store_true")
    args = parser.parse_args(argv)
    network = coerce_network(args.network)

    try:
        relay = KoraRelay.from_env()
    except KoraRelayError as exc:
        print(f"STOP: relay not reachable: {exc}")
        return 2
    buyer = AuthorityKeypairBackend(args.buyer_keypair)
    print(f"  relay (fee payer)  {relay.pubkey}")
    print(f"  buyer (authority)  {buyer.pubkey}")

    buyer_sol_before = _balance(args.rpc_url, buyer.pubkey)
    relay_sol_before = _balance(args.rpc_url, relay.pubkey)
    print(f"  buyer SOL before   {buyer_sol_before}")
    print(f"  relay SOL before   {relay_sol_before}")

    # 1. PREPARE, with the relay as payer. Every refusal the tool makes, this makes.
    out = prepare_purchase_result(
        {
            "store": args.store,
            "product": args.product,
            "buyer": buyer.pubkey,
            "fee_payer": relay.pubkey,
            "table": args.table,
            "network": network,
            "rpc_url": args.rpc_url,
        },
        # The tool's default guard refuses loopback because the hosted surface is an
        # unauthenticated proxy. This runner is the operator's own process on the
        # operator's own machine, and a fork lives on loopback by construction, so on a
        # fork the guard is the scheme check alone. Mainnet keeps the public-only guard.
        url_guard=validate_rpc_url if network == "fork" else None,
    )
    if out.get("error"):
        print(f"STOP: {out['error']}")
        return 2
    if out.get("refused"):
        print(f"REFUSED [{out.get('code')}]: {out.get('reason')}")
        return 1
    gasless = out.get("gasless") or {}
    print(f"\n  RECEIPT  {out['status'].upper()}  {out['units_consumed']:,} CU")
    print(f"  binding  {out['binding_strength']}  {out.get('binding_prefix', '')}")
    print(f"  signers  {gasless.get('signatures_required')}")
    unsigned = out["transaction"]["unsigned_transaction"]

    if not args.broadcast:
        print(
            "\nDRY RUN: bytes prepared and verified; the relay was not asked to sign."
        )
        print("Re-run with --broadcast to sponsor, co-sign, and send.")
        return 0

    # 3. SETTLE: relay -> re-verify -> buyer (authority) -> merge -> send.
    plan = out["accounts"]
    entries = plan.values() if isinstance(plan, dict) else plan
    writable = frozenset(
        entry["address"]
        for entry in entries
        if entry.get("writable") and entry["address"] != buyer.pubkey
    )
    gate = SpendPolicyGate(
        policy=default_spend_policy(
            allowed_destinations=writable,
            sponsored=True,
            usdc_per_transaction_raw=int(round(args.max_spend_usdc * 1_000_000)),
        ),
        ledger=InMemorySpendLedger(),
    )
    signer = TransactionSigner(
        backend=buyer,
        profile=SignerProfile(
            name=DEVELOPER_KEYPAIR_FILE_PROFILE_NAME,
            network=network,
            authorized=True,
            signing_as=AUTHORITY_ROLE,
        ),
        spend_gate=gate,
    )
    outcome = settle_sponsored(
        unsigned,
        network=network,
        rpc_url=args.rpc_url,
        relay=relay,
        signer=signer,
        authority=buyer.pubkey,
        last_valid_block_height=int(out["expires"]["last_valid_block_height"]),
    )
    if not isinstance(outcome, PurchaseSettled):
        print(f"\nREFUSED [{outcome.code}]: {outcome.reason}")
        return 1

    print(f"\n  LANDED   {outcome.signature}")
    print(
        f"  CU       predicted {outcome.predicted_units}  charged {outcome.consumed_units}"
    )
    print(
        f"  signers  {outcome.signatures}  payer {outcome.fee_payer[:8]}…  "
        f"authority {outcome.authority[:8]}…"
    )

    # 4. JUDGE BY WHAT MOVED.
    buyer_sol_after = _balance(args.rpc_url, buyer.pubkey)
    relay_sol_after = _balance(args.rpc_url, relay.pubkey)
    print(f"\n  buyer SOL after    {buyer_sol_after}   (before {buyer_sol_before})")
    print(f"  relay SOL after    {relay_sol_after}   (before {relay_sol_before})")
    if buyer_sol_after != buyer_sol_before:
        print("NOT GASLESS: the buyer's SOL moved.")
        return 1
    print("GASLESS: the buyer's SOL did not move; the relay paid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
