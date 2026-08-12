"""Prove the Privy hop on a MAINNET FORK, at $0: does the enclave really sign our bytes?

    uv run python scripts/privy_fork_proof.py

WHY A FORK AND NOT MAINNET. The question this answers is about ONE hop — does Privy return
a real signature over the exact message we handed it, and does the seam accept it. A fork
answers that identically to mainnet: the enclave signs whatever message it is given, and it
cannot tell which chain the blockhash came from. Everything else about the fork (its
faucet, its blockhash, its slots) is local. So this is the whole Privy question, decided for
nothing, before any real money is at stake — Pattern B.

THE FORK IS NOT MAINNET and this file never claims otherwise. It starts a local surfpool
validator lazily backed by mainnet state, funds the Privy wallet with the fork's own
cheatcodes (``surfnet_setAccount`` / ``surfnet_setTokenAccount`` — local state edits, not
transfers), and nothing here is broadcast to any real network.

What it proves, stated narrowly:
  1. Privy signs for the wallet we configured, and the signature slot comes back FILLED.
  2. The signed message is byte-identical to the one the receipt attested (the seam's
     re-binding passes over a real external signer, not a fake).
  3. The whole autonomous loop — plan refusal, simulate, bind, spend gate, sign — runs with
     no key on this machine.

What it does NOT prove: that the transaction is the one you meant, or anything at all about
mainnet state.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.landing import assemble_unsigned_tx, latest_blockhash  # noqa: E402
from gecko.pda_testkit import SurfpoolFork  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.signer import (  # noqa: E402
    EXTERNAL_SIGNER_PROFILE_NAME,
    SignerProfile,
    SignerRefused,
    TransactionSigner,
)
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.spend_policy import (  # noqa: E402
    AllowedInstruction,
    InMemorySpendLedger,
    SpendPolicy,
    SpendPolicyGate,
    TokenCaps,
)
from gecko.handoff import verify_handoff  # noqa: E402
from gecko.txbind import message_binding  # noqa: E402
from scripts.privy_backend import PrivyBackendError, first_signature  # noqa: E402
from scripts.privy_backend import from_env as privy_from_env  # noqa: E402

MAINNET_RPC = "https://api.mainnet-beta.solana.com"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
MEMO_PAYLOAD = b"gecko-privy-fork-proof"
#: One SOL in lamports, in hex — what surfnet_setAccount wants.
ONE_SOL = 1_000_000_000


def fund(rpc_url: str, address: str) -> None:
    """Give the wallet lamports on the FORK. A local state edit, never a transfer."""
    default_rpc_call(rpc_url, "surfnet_setAccount", [address, {"lamports": ONE_SOL}])


def build_memo(payer: str, rpc_url: str) -> str:
    """One memo instruction, signed by the payer and touching nothing else.

    A memo is the right subject: it moves no money, so what the run demonstrates is the
    SIGNING hop rather than a purchase, and a reader cannot mistake it for a transfer.
    """
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    blockhash, _last_valid = latest_blockhash(rpc_url)
    return assemble_unsigned_tx(
        [Instruction(Pubkey.from_string(MEMO_PROGRAM), MEMO_PAYLOAD, [])],
        payer,
        blockhash=blockhash,
    ).tx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        default="fork",
        choices=("fork", "mainnet"),
        help=(
            "fork: start a local surfpool and fund the wallet from its faucet. "
            "mainnet: simulate against the real chain, read-only. NEITHER broadcasts."
        ),
    )
    args = parser.parse_args(argv)

    try:
        backend = privy_from_env(network=args.network)
    except PrivyBackendError as exc:
        print(f"STOP: {exc}")
        return 2

    print("== the key holder")
    try:
        payer = backend.pubkey
    except PrivyBackendError as exc:
        print(f"STOP: {exc}")
        return 2
    print(f"   Privy wallet   {backend.wallet_id}")
    print(f"   address        {payer}")
    print("   private key    NOT ON THIS MACHINE — it never leaves Privy's enclave\n")

    if args.network == "fork":
        print("== starting a local surfpool fork of mainnet (this is NOT mainnet)")
        fork_context: Any = SurfpoolFork(MAINNET_RPC)
        label = "simulated on a LOCAL surfpool fork — NOT mainnet"
    else:
        print("== simulating against MAINNET, read-only (simulateTransaction only)")
        fork_context = contextlib.nullcontext()
        label = "simulated against mainnet (read-only, unsigned)"

    with fork_context as fork:
        rpc_url = fork.rpc_url if fork is not None else MAINNET_RPC
        print(f"   rpc            {rpc_url}")
        if args.network == "fork":
            fund(rpc_url, payer)
            lamports = (
                default_rpc_call(rpc_url, "getBalance", [payer]).get("result") or {}
            ).get("value")
            print(f"   funded         {lamports} lamports (fork faucet, not real SOL)")
        print()

        print("== build → simulate → bind")
        unsigned = build_memo(payer, rpc_url)
        # The bytes are already assembled, so the "builder" hands back exactly them —
        # the same seam prepare_purchase uses to simulate a transaction it already holds.
        receipt = simulate(
            {},
            rpc_url=rpc_url,
            build_call=lambda _plan: BuiltTx(tx=unsigned, encoding="base64"),
            network=args.network,
            network_label=label,
            replace_blockhash=False,
            track=(payer,),
        )
        print(f"   token leg      {receipt.token_delta.status}")
        print(f"   status         {receipt.status}")
        print(f"   compute units  {receipt.units_consumed}")
        print(
            f"   binding        {receipt.message_binding[:16]}… ({receipt.binding_strength})"
        )
        print(f"   observed slot  {receipt.observed_slot}\n")
        if receipt.status != "pass":
            print(f"STOP: the fork refused the transaction: {receipt.err}")
            return 1

        # verify_handoff, not evaluate_tx: the signer's parameter is a SignerHandoff, which
        # welds the approval to the bytes. evaluate_tx returns a verdict WITHOUT them.
        handoff = verify_handoff(
            unsigned,
            receipt,
            require="exact",
            expected_network=args.network,
            encoding="base64",
        )
        print("== the handoff")
        print(f"   approved       {handoff.approved}")
        print(f"   reason         {handoff.reason}\n")
        if not handoff.approved or handoff.transaction_base64 is None:
            return 1

        gate = SpendPolicyGate(
            policy=SpendPolicy(
                authorized=True,
                per_transaction_cap_lamports=ONE_SOL,
                hourly_cap_lamports=ONE_SOL,
                daily_cap_lamports=ONE_SOL,
                max_transactions_per_day=10,
                allowed_instructions=frozenset(
                    {
                        AllowedInstruction(
                            program_id=MEMO_PROGRAM, discriminator=MEMO_PAYLOAD[:8]
                        )
                    }
                ),
                allowed_destinations=frozenset({payer}),
                token_caps=TokenCaps.none(),
            ),
            ledger=InMemorySpendLedger(),
        )
        signer = TransactionSigner(
            backend=backend,
            profile=SignerProfile(
                name=EXTERNAL_SIGNER_PROFILE_NAME, network=args.network, authorized=True
            ),
            spend_gate=gate,
        )
        # `processed`, deliberately. simulateTransaction reports the slot it ran against,
        # which is the most recent one the node has processed. Asking getSlot at the
        # DEFAULT (finalized) commitment reads ~31 slots behind, and the signer then
        # refuses `receipt-slot-implausible` — correctly: a receipt ahead of "now" means
        # the two observations are not of the same chain. The fix belongs here, in how the
        # slot is observed, never in relaxing that check.
        current_slot = default_rpc_call(
            rpc_url, "getSlot", [{"commitment": "processed"}]
        ).get("result")
        if current_slot is None:
            print("STOP: the node did not report a current slot")
            return 1

        print("== asking Privy to sign (signTransaction — never signAndSend)")
        started = time.monotonic()
        try:
            signed = signer.sign(
                handoff, receipt=receipt, current_slot=int(current_slot)
            )
        except SignerRefused as exc:
            print(f"   REFUSED [{exc.code}] {exc}")
            return 1
        elapsed = time.monotonic() - started

        raw = base64.b64decode(signed.signed_transaction_base64)
        signature = first_signature(raw)
        print(f"   SIGNED in      {elapsed:.2f}s")
        print(f"   signer         {signed.signer_pubkey}")
        print(f"   signature      {base64.b64encode(signature).decode()[:32]}…")
        print(f"   slot filled    {signature != bytes(64)}")
        print(
            "   message same   "
            f"{message_binding(raw, strength='exact') == receipt.message_binding}"
        )
        print(f"   spend verdict  authorized={signed.spend_verdict.authorized}\n")

        print("== what this proves, and what it does not")
        print("   PROVEN : Privy returned a real signature over the exact message the")
        print(
            "            receipt attested, and the seam accepted it. No key on this box."
        )
        print("   NOT    : that this is mainnet (it is a local fork), and not that the")
        print(
            "            transaction is the one you meant — a receipt says well-formed."
        )
        print(
            "   NOT SENT: nothing here broadcasts. The signed bytes stay in this process."
        )
        print(json.dumps({"signed": True, "network": args.network, "broadcast": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
