"""TxLINE on-chain subscribe — simulate (default) or broadcast (--broadcast).

Builds the `subscribe(service_level_id, weeks)` instruction from the IDL and
either SIMULATES it on mainnet (no funds, no signature needed) or BROADCASTS the
real tx signed by the dedicated subscriber keypair.

Run (ephemeral deps, no project pollution):
  uv run --with solders --with httpx python scripts/subscribe.py            # simulate
  uv run --with solders --with httpx python scripts/subscribe.py --broadcast # real (founder-gated)

Free World Cup tier: service_level_id 1 (60s delay) or 12 (real-time); weeks 4; no TxL spent (just gas).
"""

from __future__ import annotations

import base64
import json
import os
import sys

import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from gecko.mainnet_ledger import LedgerRow
from gecko.mainnet_ledger import record as record_ledger
from solders.transaction import Transaction

RPC = os.environ.get("RPC", "https://api.mainnet-beta.solana.com")
KEYPAIR_PATH = os.path.expanduser("~/.gecko/wallets/txodds-subscriber.json")

PROGRAM = Pubkey.from_string("9ExbZjAapQww1vfcisDmrngPinHTEfpjYRWMunJgcKaA")
TXL_MINT = Pubkey.from_string("Zhw9TVKp68a1QrftncMSd6ELXKDtpVMNuMGr1jNwdeL")
TOKEN_2022 = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM = Pubkey.from_string("11111111111111111111111111111111")

SERVICE_LEVEL_ID = int(
    os.environ.get("SERVICE_LEVEL_ID", "12")
)  # 12 = real-time (free)
WEEKS = int(os.environ.get("WEEKS", "4"))
DISCRIMINATOR = bytes([254, 28, 191, 138, 156, 179, 183, 53])


def pda(seeds: list[bytes]) -> Pubkey:
    return Pubkey.find_program_address(seeds, PROGRAM)[0]


def ata(owner: Pubkey, mint: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_2022), bytes(mint)], ATA_PROGRAM
    )[0]


def create_ata_idempotent_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    """ATA-program CreateIdempotent (instruction 1) for a Token-2022 ATA.

    subscribe() expects user_token_account to already exist (AnchorError 3012),
    so we create it in the same tx — idempotent, so it's a no-op if it exists.
    """
    metas = [
        AccountMeta(payer, True, True),
        AccountMeta(ata(owner, mint), False, True),
        AccountMeta(owner, False, False),
        AccountMeta(mint, False, False),
        AccountMeta(SYSTEM, False, False),
        AccountMeta(TOKEN_2022, False, False),
    ]
    return Instruction(ATA_PROGRAM, bytes([1]), metas)


def rpc(method: str, params: list) -> dict:
    r = httpx.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    return r.json()


def build_ix(user: Pubkey) -> Instruction:
    pricing_matrix = pda([b"pricing_matrix"])
    treasury_pda = pda([b"token_treasury_v2"])
    user_ta = ata(user, TXL_MINT)
    treasury_vault = ata(treasury_pda, TXL_MINT)
    data = (
        DISCRIMINATOR
        + SERVICE_LEVEL_ID.to_bytes(2, "little")
        + WEEKS.to_bytes(1, "little")
    )
    metas = [
        AccountMeta(user, True, True),
        AccountMeta(pricing_matrix, False, False),
        AccountMeta(TXL_MINT, False, False),
        AccountMeta(user_ta, False, True),
        AccountMeta(treasury_vault, False, True),
        AccountMeta(treasury_pda, False, False),
        AccountMeta(TOKEN_2022, False, False),
        AccountMeta(SYSTEM, False, False),
        AccountMeta(ATA_PROGRAM, False, False),
    ]
    print("derived accounts:")
    print("  user            ", user)
    print("  pricing_matrix  ", pricing_matrix)
    print("  user_token_acct ", user_ta)
    print("  treasury_vault  ", treasury_vault)
    print("  treasury_pda    ", treasury_pda)
    return Instruction(PROGRAM, data, metas)


def main() -> int:
    kp = Keypair.from_bytes(bytes(json.load(open(KEYPAIR_PATH))))
    user = kp.pubkey()
    print(f"subscriber: {user}  | service_level={SERVICE_LEVEL_ID} weeks={WEEKS}\n")
    ix = build_ix(user)
    instructions = [create_ata_idempotent_ix(user, user, TXL_MINT), ix]
    broadcast = "--broadcast" in sys.argv

    if not broadcast:
        tx = Transaction.new_signed_with_payer(instructions, user, [kp], Hash.default())
        b64 = base64.b64encode(bytes(tx)).decode()
        res = rpc(
            "simulateTransaction",
            [
                b64,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "processed",
                },
            ],
        )
        val = res.get("result", {}).get("value", res)
        err = val.get("err") if isinstance(val, dict) else val
        print("\n=== SIMULATION ===")
        print("err:", err)
        print(
            "unitsConsumed:", val.get("unitsConsumed") if isinstance(val, dict) else "?"
        )
        for line in (val.get("logs") or [])[-20:] if isinstance(val, dict) else []:
            print("  ", line)
        print(
            "\nRESULT:",
            "PASS — safe to broadcast"
            if err is None
            else "FAIL — do not broadcast (see logs)",
        )
        return 0 if err is None else 1

    # broadcast (founder-gated) -> confirm -> activate -> print tokens
    import time

    bh = rpc("getLatestBlockhash", [{"commitment": "finalized"}])["result"]["value"][
        "blockhash"
    ]
    tx = Transaction.new_signed_with_payer(
        instructions, user, [kp], Hash.from_string(bh)
    )
    res = rpc(
        "sendTransaction",
        [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}],
    )
    txsig = res.get("result")
    print("\n=== BROADCAST ===")
    if not txsig:
        print(json.dumps(res, indent=2))
        return 1
    print("txSig:", txsig)
    # Recorded so the ledger's "one line per transaction WE sent" stays true. No
    # prediction is captured here — this script takes no receipt before signing — and
    # `predicted_cu=None` says exactly that, which keeps `with_prediction` honest rather
    # than inflating it with a row nothing was predicted for.
    written = record_ledger(
        LedgerRow(
            signature=str(txsig),
            predicted_cu=None,
            predicted_source="scripts/subscribe.py (no receipt taken before signing)",
            network="mainnet" if "mainnet" in RPC else "unknown",
        )
    )
    if written is not None:
        print("logged:", written)
    for _ in range(40):
        st = (
            rpc("getSignatureStatuses", [[txsig]])
            .get("result", {})
            .get("value", [None])[0]
        )
        if st and st.get("err"):
            print("tx failed on-chain:", st["err"])
            return 1
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            print("confirmed.")
            break
        time.sleep(2)

    base = "https://txline.txodds.com"
    jwt = httpx.post(f"{base}/auth/guest/start", timeout=30).json()["token"]
    msg = f"{txsig}::{jwt}".encode()  # leagues empty -> "txSig::jwt"
    sig_b64 = base64.b64encode(bytes(kp.sign_message(msg))).decode()
    act = httpx.post(
        f"{base}/api/token/activate",
        json={"txSig": txsig, "walletSignature": sig_b64, "leagues": []},
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=30,
    )
    token = (
        act.text.strip().strip('"')
        if "text/plain" in act.headers.get("content-type", "")
        else act.json().get("token")
    )
    sess_path = os.path.expanduser("~/.gecko/txodds-session.json")
    with open(sess_path, "w") as fh:
        json.dump({"jwt": jwt, "api_token": token}, fh)
    os.chmod(sess_path, 0o600)
    print("\n=== ACTIVATED — live session ready ===")
    print("  saved ->", sess_path, "(Gecko auto-uses it; token not printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
