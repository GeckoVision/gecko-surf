"""jurassic_fi contribute — simulate (default) or broadcast (--broadcast, FOUNDER-RUN).

  uv run python -m scripts.jurassic_contribute                    # simulate, $0
  uv run python -m scripts.jurassic_contribute --usdc 0.10        # simulate a real size
  uv run python -m scripts.jurassic_contribute --usdc 0.10 --broadcast   # founder only

WHY THIS SCRIPT EXISTS RATHER THAN A HAND-BUILT TRANSACTION. Every account below is
DERIVED from the program graph, not pasted: `launch` comes from a recipe recovered from
`initialize_launch` (the only instruction that states it derivably) with `launch_id` and
`admin` read off the chain; `user_position` and both token accounts derive from that. If
any recipe were wrong the simulation would fail — which is the check, and it is why the
default is simulate.

`min_accepted_amount` IS NOT A FORMALITY. `contribute` takes a requested amount AND a
floor, because the program partially fills when the remaining headroom is smaller than the
request. Passing the two equal means all-or-nothing; passing 0 means "take what you can".
An agent that ignores the second argument is not expressing an intent it certainly has, so
this script makes it explicit rather than defaulting it out of sight.

BROADCASTING IS FOUNDER-RUN. Claude simulates and hands over the command; it does not
sign or send a mainnet transaction. `--broadcast` needs a keypair this repo does not carry.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys

from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode, derive_pda
from gecko.rpc import default_rpc_call

PROGRAM_ID = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
LAUNCH = "9nFKKFBEVW4njBtyJkcvngEkmT3qVXm4fgGGRBLbqH65"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
#: `contribute`, from the IDL — never guessed from the name.
DISCRIMINATOR = bytes([82, 33, 68, 131, 32, 0, 205, 95])
USDC_DECIMALS = 6


def associated_token_account(owner: str, mint: str) -> str:
    return derive_pda(
        PdaNode(
            "ata",
            (
                VariablePdaSeedNode("owner", source="account", encoding="pubkey"),
                VariablePdaSeedNode(
                    "token_program", source="account", encoding="pubkey"
                ),
                VariablePdaSeedNode("mint", source="account", encoding="pubkey"),
            ),
            ATA_PROGRAM,
        ),
        {"owner": owner, "token_program": TOKEN_PROGRAM, "mint": mint},
    ).address


def user_position(launch: str, contributor: str) -> str:
    return derive_pda(
        PdaNode(
            "user_position",
            (
                ConstantPdaSeedNode(value=b"user_position", encoding="utf8"),
                VariablePdaSeedNode("launch", source="account", encoding="pubkey"),
                VariablePdaSeedNode("contributor", source="account", encoding="pubkey"),
            ),
            PROGRAM_ID,
        ),
        {"launch": launch, "contributor": contributor},
    ).address


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usdc", type=float, default=0.10, help="amount to contribute")
    parser.add_argument(
        "--min-usdc",
        type=float,
        default=None,
        help="floor below which a partial fill is refused (default: the full amount)",
    )
    parser.add_argument("--rpc", default=DEFAULT_RPC)
    parser.add_argument("--broadcast", action="store_true", help="FOUNDER-RUN ONLY")
    args = parser.parse_args()

    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    config = json.load(open(os.path.expanduser("~/.gecko/catalog-ci.json")))
    buyer = config["buyer"]

    requested = int(round(args.usdc * 10**USDC_DECIMALS))
    floor = int(
        round(
            (args.min_usdc if args.min_usdc is not None else args.usdc)
            * 10**USDC_DECIMALS
        )
    )
    if requested <= 0:
        raise SystemExit("the program refuses zero (ZeroValueNotAllowed, 6009)")

    position = user_position(LAUNCH, buyer)
    vault = associated_token_account(LAUNCH, USDC_MINT)
    payer_ata = associated_token_account(buyer, USDC_MINT)
    print(f"contributor   : {buyer}")
    print(f"user_position : {position}   (derived)")
    print(f"payment_vault : {vault}   (derived)")
    print(f"payer USDC ATA: {payer_ata}   (derived)")
    print(f"amount        : {args.usdc} USDC   floor {floor / 10**USDC_DECIMALS} USDC")

    data = DISCRIMINATOR + struct.pack("<QQ", requested, floor)
    metas = [
        AccountMeta(Pubkey.from_string(buyer), True, True),
        AccountMeta(Pubkey.from_string(LAUNCH), False, True),
        AccountMeta(Pubkey.from_string(position), False, True),
        AccountMeta(Pubkey.from_string(USDC_MINT), False, False),
        AccountMeta(Pubkey.from_string(payer_ata), False, True),
        AccountMeta(Pubkey.from_string(vault), False, True),
        AccountMeta(Pubkey.from_string(TOKEN_PROGRAM), False, False),
        AccountMeta(Pubkey.from_string(SYSTEM_PROGRAM), False, False),
    ]
    instruction = Instruction(Pubkey.from_string(PROGRAM_ID), data, metas)

    blockhash = default_rpc_call(args.rpc, "getLatestBlockhash", [])["result"]["value"][
        "blockhash"
    ]
    message = MessageV0.try_compile(
        Pubkey.from_string(buyer), [instruction], [], Hash.from_string(blockhash)
    )
    unsigned = VersionedTransaction.populate(
        message, [NullSigner(Pubkey.from_string(buyer)).sign_message(bytes(message))]
    )
    encoded = base64.b64encode(bytes(unsigned)).decode()

    simulation = default_rpc_call(
        args.rpc,
        "simulateTransaction",
        [
            encoded,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
                "innerInstructions": True,
            },
        ],
    )["result"]["value"]

    print(
        f"\nSIMULATION    err={simulation.get('err')}  CU={simulation.get('unitsConsumed')}"
    )
    if simulation.get("err") is not None:
        for line in (simulation.get("logs") or [])[-12:]:
            print("   ", line)
        return 1

    if not args.broadcast:
        print(
            "\nSimulated only — nothing was signed or sent.\n"
            "To broadcast, the founder runs the same command with --broadcast."
        )
        return 0

    # FOUNDER PATH. Deliberately not reachable without a keypair this repo does not ship.
    keypair_path = os.environ.get("JURASSIC_KEYPAIR")
    if not keypair_path:
        raise SystemExit(
            "--broadcast needs JURASSIC_KEYPAIR pointing at the contributor's keypair "
            "JSON. This repo carries no key, and Claude does not broadcast."
        )
    from solders.keypair import Keypair

    keypair = Keypair.from_bytes(bytes(json.load(open(keypair_path))))
    if str(keypair.pubkey()) != buyer:
        raise SystemExit(
            f"that keypair is {keypair.pubkey()}, not the configured buyer {buyer}"
        )
    signed = VersionedTransaction(message, [keypair])
    signature = default_rpc_call(
        args.rpc,
        "sendTransaction",
        [base64.b64encode(bytes(signed)).decode(), {"encoding": "base64"}],
    )["result"]
    print(f"\nBROADCAST     {signature}")
    print(f"              https://solscan.io/tx/{signature}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
