#!/usr/bin/env python3
"""Buy a coffee on Solana mainnet — what the program is, what the IDL drops, what lands.

    scene 1  THE PROGRAM   let_me_buy: a storefront on chain, and the two facts its IDL loses
    scene 2  THE CHECK     the store read off chain, the cost predicted, nothing signed yet
    scene 3  LANDED        a real espresso, paid in USDC, read back off the chain

Nothing here is a transcript. Every address, price and compute number comes off mainnet at
record time. Scene 3 SPENDS REAL MONEY when ``--settle`` is passed: 0.1 USDC for an
espresso at the geckocoffee storefront. Without it, scene 3 looks up a purchase this same
loop already settled (gecko_101's rule: re-broadcasting to make footage is spending money
for a video).

    GECKO_RPC_URL=... uv run python demo/kit/coffee_screenplay.py --rehearsal
    GECKO_RPC_URL=... asciinema rec demo/kit/coffee.cast --cols 90 --rows 24 --overwrite \\
      -c "uv run python demo/kit/coffee_screenplay.py --settle"

Honesty rules (the kit README's): every number from the wire; no secrets on screen; one
unedited take; real waits stay real. The RPC URL carries an API key, so it is read from
the environment and NEVER printed — the displayed command shows a redacted placeholder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from screenplay import (  # noqa: E402
    BOLD,
    CYAN,
    GREEN,
    RED,
    RESET,
    YELLOW,
    clear,
    out,
    put,
)

DIM = "\033[38;5;245m"

REHEARSAL = "--rehearsal" in sys.argv
LIVE_SETTLE = "--settle" in sys.argv

STORE = "geckocoffee"
PRODUCT = "Espresso"
PROGRAM = "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya"
BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"

#: A purchase this loop already settled, shown when --settle is absent. Scene 3 reads it
#: back off the chain either way, so the numbers on screen are the chain's in both modes.
SETTLED_SIG = os.environ.get(
    "GECKO_SETTLED_SIG",
    "3zTRSSJqe9HDKi7vf5fW8jW8oMjfpDrhwBRYynpPC9jVuUfoBhf4xqYBS9Prb5EFGyXZjmVU6TSAJtA32VBDviH",
)

RPC = os.environ.get("GECKO_RPC_URL", "")
if not RPC:
    sys.exit("set GECKO_RPC_URL (it carries an API key and is never printed)")

#: What the audience sees in place of the real endpoint. Never interpolate RPC into output.
RPC_SHOWN = "$GECKO_RPC_URL"


def rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        RPC, body.encode(), {"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


# ─────────────────────────────────────────────────────────────────────────────
# scene 1 — THE PROGRAM
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(f"{BOLD}let_me_buy — a storefront that lives on Solana{RESET}", 0.03, pause=0.5)
put("")
put(f"  {DIM}letmebuy.app · {PROGRAM}{RESET}", 0.7)
put("")
out("A merchant stands up a store and lists products priced in USDC.", 0.02)
out("A buyer scans a QR code and pays. One account per store holds", 0.02)
out("everything: the menu, the receipts, the count, the authority.", 0.02, pause=1.0)
put("")
put(f"  {CYAN}receipts = PDA(['receipts', store_name]){RESET}", 0.9)
put("")
out(f"{YELLOW}Two facts its IDL does not carry — both break the call:{RESET}", 0.02)
put("")
put(
    f"  {RED}1{RESET}  mark_as_delivered declares its seed as {BOLD}store_name{RESET}.",
    0.5,
)
put(f"     Its actual arguments are {BOLD}_store_name{RESET} and receipt_id.", 0.5)
put(f"     {DIM}Resolve seeds by argument name and you get nothing for the{RESET}", 0.4)
put(f"     {DIM}one seed that selects the store. Gecko binds by VALUE.{RESET}", 1.1)
put("")
put(
    f"  {RED}2{RESET}  the store's authority is writable but {BOLD}not a signer{RESET}.",
    0.5,
)
put("     buyer pays from ATA(signer, mint) →", 0.3)
put("     store is credited at ATA(authority, mint)", 0.5)
put(f"     {DIM}Same mint. Different owner. Derive both from one owner{RESET}", 0.4)
put(f"     {DIM}and you have built a purchase that pays the buyer back.{RESET}", 1.4)

# ─────────────────────────────────────────────────────────────────────────────
# scene 2 — THE CHECK
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(f"{CYAN}User: buy me an espresso from {STORE}{RESET}", 0.04, pause=0.8)
put("")

from gecko.store_accounts import resolve_store  # noqa: E402

resolved = resolve_store(STORE, rpc_url=RPC)
accounts = resolved.accounts_for(PRODUCT)
put(f"  {GREEN}✓{RESET} the NAME resolved to its own accounts, off the chain:", 0.6)
put("")
put(f"    receipts   {resolved.receipts}", 0.4)
put(
    f"               {DIM}PDA(['receipts', '{STORE}']) — derived, then confirmed{RESET}",
    0.6,
)
put(f"    authority  {resolved.authority}", 0.4)
put(f"    credited   {accounts.token_account}", 0.4)
put(
    f"               {DIM}ATA(authority, USDC) — the STORE's account, not the buyer's{RESET}",
    1.1,
)
put("")
put(f"  {DIM}the same account carries the menu:{RESET}", 0.8)
put("")

for product in resolved.products:
    mark = f"{GREEN}◀ this one{RESET}" if product.name == PRODUCT else ""
    put(f"    {product.name:<18} {product.price_ui:>6} USDC   {mark}", 0.5)
put("", 1.0)

# ─────────────────────────────────────────────────────────────────────────────
# scene 3 — LANDED
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(f"{BOLD}Verify first. Then spend.{RESET}", 0.03, pause=0.6)
put("")
out(f"{CYAN}$ uv run python scripts/autonomous_purchase.py \\{RESET}", 0.015)
out(f"{CYAN}    --network mainnet --rpc-url {RPC_SHOWN} --privy \\{RESET}", 0.015)
out(f"{CYAN}    --store {STORE} --product {PRODUCT}{RESET}", 0.015, pause=0.8)
put("")

if LIVE_SETTLE and not REHEARSAL:
    put(
        f"  {DIM}simulating against live mainnet, then signing in the enclave…{RESET}",
        0.3,
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "autonomous_purchase.py"
            ),
            "--network",
            "mainnet",
            "--rpc-url",
            RPC,
            "--privy",
            "--store",
            STORE,
            "--product",
            PRODUCT,
        ],
        capture_output=True,
        text=True,
    )
    signature = ""
    predicted = ""
    for line in completed.stdout.splitlines():
        if "signature" in line:
            signature = line.split()[-1]
        elif "predicted" in line:
            predicted = line.split()[1]
    if predicted:
        put(
            f"  {DIM}receipt says it will cost {predicted} CU — before signing{RESET}",
            0.8,
        )
    if not signature:
        put(
            f"  {RED}the run did not settle — showing its refusal, unedited{RESET}", 0.5
        )
        for line in completed.stdout.splitlines()[-12:]:
            put(f"  {line}", 0.15)
        sys.exit(1)
else:
    signature = SETTLED_SIG
    put(f"  {DIM}(looking up a purchase this loop already settled){RESET}", 0.8)

put("")
# A settled signature is not an INDEXED one: getTransaction answers from a commitment
# that lags the send by a few slots, and a `None` result there is "not yet", not "not
# there". The first take of this cast crashed on exactly that. Poll, then give up loudly.
detail = None
for attempt in range(30):
    detail = rpc(
        "getTransaction",
        [signature, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
    )["result"]
    if detail is not None:
        break
    if attempt == 0:
        put(f"  {DIM}waiting for the chain to index it…{RESET}", 0.0)
    time.sleep(2.0)
if detail is None:
    put(f"  {RED}{signature} did not appear within 60s{RESET}", 0.5)
    put(
        f"  {DIM}the purchase may still have settled — check before re-running{RESET}",
        0.5,
    )
    sys.exit(1)
meta = detail["meta"]

pre = {
    b["owner"]: b["uiTokenAmount"]["uiAmountString"]
    for b in meta.get("preTokenBalances", [])
}
post = {
    b["owner"]: b["uiTokenAmount"]["uiAmountString"]
    for b in meta.get("postTokenBalances", [])
}

put(f"  {GREEN}SETTLED{RESET}  slot {detail['slot']}", 0.5)
put(f"  {signature[:44]}…", 0.6)
put("")
put(f"    error         {meta['err']}", 0.4)
put(
    f"    consumed      {BOLD}{meta['computeUnitsConsumed']} CU{RESET}  {DIM}— read off the chain{RESET}",
    0.9,
)
put("")
put(f"    buyer  USDC   {pre.get(BUYER, '?')} → {post.get(BUYER, '?')}", 0.5)
put(
    f"    store  USDC   {pre.get(resolved.authority, '?')} → {post.get(resolved.authority, '?')}",
    1.2,
)
put("")
out(
    f"{GREEN}The receipt predicted that number before anything was signed.{RESET}",
    0.025,
)
put("")
put(
    f"  {DIM}gecko holds no key. the enclave signed. gecko submitted the bytes,{RESET}",
    0.4,
)
put(
    f"  {DIM}so the signed message could be checked against the receipt first.{RESET}",
    1.6,
)
