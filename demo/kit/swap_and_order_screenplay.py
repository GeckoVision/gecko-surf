#!/usr/bin/env python3
"""SWAP, THEN ORDER — a USDG->USDC swap and a three-item coffee order, on a mainnet fork.

    scene 1  THE SWAP      0.25 USDG -> USDC on Orca Whirlpool. The first attempt is
                           REFUSED before signing (the buyer's USDC account does not
                           exist yet); the account is created; the second lands.
    scene 2  THE ORDER     "a black coffee and a water, please." "oh, and a cappuccino."
                           31 items whose names lie; categories derived from attributes;
                           three purchases prepared through the production path, signed
                           by a throwaway key, landed, and judged by what moved.
    scene 3  THE RECEIPTS  both read back off the fork by signature.

EVERYTHING RUNS ON A SURFPOOL FORK OF MAINNET. $0. Nothing reaches the chain. Every
number on screen is what the fork answered during the take; the fork is named on every
scene. The buyer keys are throwaway: generated inside this process after the fork has
proved itself, funded by cheatcode, never reused.

    # terminal 1: the fork (the RPC URL carries an API key and is never printed)
    surfpool start --no-tui --no-deploy --rpc-url "$GECKO_MAINNET_RPC" --port 8899
    # seed the 31-item menu on the fork once
    uv run python scripts/semantic_seed_fork.py --rpc-url http://127.0.0.1:8899

    # terminal 2: rehearse, then record
    uv run python demo/kit/swap_and_order_screenplay.py
    asciinema rec --cols 80 --rows 20 --overwrite demo/kit/swap_and_order.cast \\
        -c "uv run python demo/kit/swap_and_order_screenplay.py"
    uv run --with pyte --with pillow python demo/kit/render_cast.py \\
        demo/kit/swap_and_order.cast demo/kit/swap_and_order.mp4 --scale 2 \\
        --brand "GECKO  •  CHECK THE CALL BEFORE IT COUNTS" \\
        --scene "Gecko — swap USDG to USDC|surfpool fork of mainnet · refused, fixed, landed" \\
        --scene "Gecko — a black coffee, and water|31 items, names that lie · attributes decide" \\
        --scene "Gecko — the receipts|read back off the fork by signature"

Honesty rules (demo/kit/README.md, law): one unedited take; the fork is labelled wherever
a number appears; the refusal in scene 1 is the program's own error, unedited.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

DIM = "\033[38;5;245m"

REPO = Path(__file__).resolve().parents[2]
FORK = os.environ.get("GECKO_FORK_RPC", "http://127.0.0.1:8899")
FORK_SHOWN = "$FORK_RPC"
STORE = "geckocoffee"
USDG = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SWAP_IN = 250_000  # 0.25 USDG

from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.sandbox.cheatcodes import fund_sol, fund_token  # noqa: E402
from gecko.sandbox.rehearse import rehearse_purchase  # noqa: E402
from gecko.sandbox.surfnet import ephemeral_signer, prove_surfnet  # noqa: E402
from gecko.semantic_catalogue import (  # noqa: E402
    BLACK_COFFEE_DEFAULT,
    WATER_DEFAULT,
    category_members,
    get_item,
)
from gecko.semantic_fork_surface import ForkStoreSurface  # noqa: E402
from gecko.semantic_gate import OrderConstraints, plan_gate  # noqa: E402
from gecko.store_accounts import receipts_pda  # noqa: E402
from solders.keypair import Keypair  # noqa: E402


def rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        FORK, body.encode(), {"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)["result"]


def short(value: str, keep: int = 8) -> str:
    return f"{value[:keep]}…"


def usdc(raw: int) -> str:
    return f"{raw / 1_000_000:.2f}"


def fork_banner() -> None:
    slot = rpc("getSlot", [])
    put(
        f"{DIM}  surfpool fork of mainnet · slot {slot:,} · $0 · nothing reaches mainnet{RESET}",
        0.6,
    )
    put("")


# ─────────────────────────────────────────────────────────────────────────────
# scene 1 — the swap: refused, fixed, landed
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(f"{BOLD}Swap USDG → USDC{RESET}", 0.03, pause=0.3)
fork_banner()

proof = prove_surfnet(FORK)
keypair = Keypair()
buyer = str(keypair.pubkey())
key_dir = Path(tempfile.mkdtemp(prefix="gecko-fork-buyer-"))
key_file = key_dir / "buyer.json"
key_file.write_text(json.dumps(list(bytes(keypair))))
key_file.chmod(0o600)

out(f"{CYAN}$ agent: swap 0.25 USDG to USDC for {short(buyer)}{RESET}", 0.03, pause=0.4)
fund_sol(proof, buyer, 50_000_000)
funded = fund_token(proof, buyer, USDG, SWAP_IN, token_program=TOKEN_2022)
put(
    f"  {GREEN}✓{RESET} fork proven · throwaway buyer funded by cheatcode: 0.05 SOL, "
    f"{funded.observed_amount:,} USDG",
    0.9,
)
put("")


def swap_attempt() -> tuple[str, str]:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "prepare_whirlpool_swap.py"),
            "--signer",
            buyer,
            "--rpc-url",
            FORK,
            "--network",
            "fork",
            "--direction",
            "a-to-b",
            "--amount",
            str(SWAP_IN),
            "--send",
            "--keypair",
            str(key_file),
        ],
        capture_output=True,
        text=True,
    )
    return completed.stdout, completed.stderr


out(
    f"{CYAN}$ prepare_whirlpool_swap --direction a-to-b --amount {SWAP_IN} --send{RESET}",
    0.02,
    pause=0.3,
)
stdout, _ = swap_attempt()
if "REFUSED" in stdout:
    anchor = re.search(
        r"AnchorError caused by account: (\S+)\. Error Code: (\w+)\. Error Number: (\d+)",
        stdout,
    )
    put(f"  {RED}✗ REFUSED before signing{RESET}", 0.4)
    if anchor:
        put(
            f"    {RED}{anchor.group(1)}: {anchor.group(2)} ({anchor.group(3)}){RESET}",
            0.8,
        )
    put(
        f"  {YELLOW}the swap's output account does not exist yet. nothing was signed.{RESET}",
        1.4,
    )
    put("")
    out(
        f"{CYAN}$ spl-token create-account USDC --owner {short(buyer)} -u {FORK_SHOWN}{RESET}",
        0.02,
        pause=0.2,
    )
    created = subprocess.run(
        [
            "spl-token",
            "create-account",
            USDC,
            "--program-id",
            TOKEN_CLASSIC,
            "--owner",
            buyer,
            "--fee-payer",
            str(key_file),
            "-u",
            FORK,
        ],
        capture_output=True,
        text=True,
    )
    ok = created.returncode == 0
    put(
        f"  {GREEN if ok else RED}{'✓' if ok else '✗'} {'created' if ok else 'failed'}: "
        f"{created.stdout.strip().splitlines()[-1][:60] if created.stdout.strip() else created.stderr.strip()[:60]}{RESET}",
        0.8,
    )
    put("")
    out(
        f"{CYAN}$ prepare_whirlpool_swap … --send   (same call, again){RESET}",
        0.02,
        pause=0.3,
    )
    stdout, _ = swap_attempt()
else:
    put(
        f"  {YELLOW}the first attempt was not refused this time — recording continues honestly{RESET}",
        1.0,
    )

swap_sig = ""
clean = re.search(r"SIMULATED CLEAN\s+(\d+) compute units", stdout)
binding = re.search(r"binding\s+([0-9a-f]{64}) \[(\w+)\]", stdout)
sent = re.search(r"SENT\s+(\S+)", stdout)
if clean and binding:
    put(
        f"  {GREEN}✓ simulated clean{RESET}  {BOLD}{int(clean.group(1)):,} CU{RESET}  ·  binding "
        f"{binding.group(1)[:12]}… [{binding.group(2)}]",
        0.6,
    )
if sent:
    swap_sig = sent.group(1)
    put(f"  {GREEN}✓ sent{RESET}  {DIM}{short(swap_sig, 44)}{RESET}", 0.6)
else:
    put(
        f"  {RED}✗ not sent — the pre-flight did not approve it. shown, not hidden{RESET}",
        1.0,
    )
    tail = [line for line in stdout.splitlines() if line.strip()][-3:]
    for line in tail:
        put(f"    {DIM}{line.strip()[:74]}{RESET}", 0.3)
put("", 1.2)

# ─────────────────────────────────────────────────────────────────────────────
# scene 2 — the order: names that lie, attributes that decide, three landings
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(f"{BOLD}A black coffee, and water{RESET}", 0.03, pause=0.3)
fork_banner()

out(f'{CYAN}User: "a black coffee and a water, please."{RESET}', 0.04, pause=0.5)
out(f'{CYAN}User: "oh, and a cappuccino."{RESET}', 0.04, pause=0.7)
put("")

surface = ForkStoreSurface(
    proof=proof,
    store_name=STORE,
    store_address=receipts_pda(STORE),
    rpc_call=default_rpc_call,
    out_of_stock=frozenset(),
)
listing = surface._listing_now()  # noqa: SLF001 - the menu, read off the fork
black = category_members("hot_black_coffee")
water = category_members("plain_water")
black_ids = {item.item_id for item in black}
water_ids = {item.item_id for item in water}
from gecko.semantic_catalogue import BY_ID  # noqa: E402

KEYWORDS = ("black", "water", "espresso", "coffee")
name_traps = sorted(
    item.name
    for item in BY_ID.values()
    if any(word in item.name.lower() for word in KEYWORDS)
    and item.item_id not in black_ids | water_ids
)
put(f"  menu read off the fork: {len(listing.products)} items on {STORE}", 0.4)
put("  names that lie to a keyword match:", 0.2)
put(f"    {', '.join(name_traps)}"[:80], 0.9)
put("")
put(
    f'  {GREEN}✓{RESET} "black coffee" → hot_black_coffee, derived from attributes only',
    0.4,
)
put(f"    (coffee · hot · no milk · not sweetened): {len(black)} members", 0.5)
names = [item.name for item in black]
put(f"    {', '.join(names[:3])},", 0.4)
put(f"    {', '.join(names[3:])}", 0.8)
put(
    f'  {GREEN}✓{RESET} "water" → plain_water: {", ".join(item.name for item in water)}',
    0.9,
)
put("")

order = (BLACK_COFFEE_DEFAULT, WATER_DEFAULT, "cappuccino")
put("  basket, house defaults where the user did not choose:", 0.2)
total = 0
for item_id in order:
    live = surface.read_item(item_id)
    total += live.price_lamports
    put(f"    {get_item(item_id).name:<22} {usdc(live.price_lamports):>6} USDC", 0.3)
put(f"    {'total':<22} {usdc(total):>6} USDC", 0.6)
decision = plan_gate(order, OrderConstraints())
put(
    f"  {GREEN if decision.allow else RED}gate: {'allow' if decision.allow else 'block'}{RESET}"
    f"{'' if decision.allow else '  ' + decision.reason_text()[:60]}",
    0.8,
)
put("")

landed: list[tuple[str, str]] = []
authority = surface.authority()
if decision.allow:
    for item_id in order:
        name = get_item(item_id).name
        put(f"  {DIM}{name}: fund → prepare → sign → land → judge…{RESET}", 0.1)
        rehearsal = rehearse_purchase(
            proof,
            buyer=ephemeral_signer(proof),
            store=STORE,
            product=name,
            rpc_call=default_rpc_call,
        )
        if rehearsal.landed and not rehearsal.discrepancies and rehearsal.signature:
            landed.append((name, rehearsal.signature))
            bt = rehearsal.buyer_token.moved if rehearsal.buyer_token else None
            st = rehearsal.store_token.moved if rehearsal.store_token else None
            put(
                f"  {GREEN}✓ {name:<21}{RESET} predicted {BOLD}{rehearsal.simulated_units:,}{RESET} "
                f"= consumed {BOLD}{rehearsal.units_consumed:,} CU{RESET}",
                0.2,
            )
            receipt = rehearsal.receipt
            wrote = (
                f"receipts {receipt.total_purchases_before}→{receipt.total_purchases_after}"
                if receipt
                else "receipt ?"
            )
            put(
                f"    buyer {bt:+,} · store {st:+,} · {wrote} · "
                f"{DIM}{short(rehearsal.signature, 16)}{RESET}",
                0.5,
            )
        else:
            why = "; ".join(rehearsal.discrepancies) or "did not land"
            put(f"  {RED}✗ {name}: {why[:70]}{RESET}", 0.8)
    put("")
    colour = GREEN if len(landed) == len(order) else YELLOW
    put(
        f"{colour}  -> {len(landed)} of {len(order)} landed · every credit to the store "
        f"authority · books balance{RESET}",
        1.6,
    )

# ─────────────────────────────────────────────────────────────────────────────
# scene 3 — the receipts, read back by signature
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(
    f"{BOLD}The receipts — read back off the fork, by signature{RESET}", 0.03, pause=0.3
)
fork_banner()


def read_back(label: str, signature: str, owner: str) -> None:
    detail = rpc(
        "getTransaction",
        [signature, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
    )
    if detail is None:
        put(f"  {RED}{label}: {short(signature, 20)} not found on the fork{RESET}", 0.8)
        return
    meta = detail["meta"]
    pre = {
        (b["owner"], b["mint"]): int(b["uiTokenAmount"]["amount"])
        for b in meta.get("preTokenBalances", [])
    }
    post = {
        (b["owner"], b["mint"]): int(b["uiTokenAmount"]["amount"])
        for b in meta.get("postTokenBalances", [])
    }
    put(f"  {BOLD}{label}{RESET}  {DIM}{short(signature, 32)}{RESET}", 0.3)
    put(
        f"    slot {detail['slot']:,} · error {meta['err']} · consumed {BOLD}{meta['computeUnitsConsumed']:,} CU{RESET}",
        0.5,
    )
    for acct_owner, mint in sorted(set(pre) | set(post)):
        if acct_owner != owner:
            continue
        symbol = "USDG" if mint == USDG else "USDC" if mint == USDC else short(mint, 6)
        put(
            f"    {symbol:<5} {pre.get((acct_owner, mint), 0):>9,} → {post.get((acct_owner, mint), 0):>9,}",
            0.35,
        )
    put("")


if swap_sig:
    read_back("swap USDG → USDC", swap_sig, buyer)
if landed:
    name, sig = landed[-1]
    # The purchase's buyer is the ephemeral key; the STORE side is the stable party.
    read_back(f"purchase: {name}", sig, authority)

out(f"{GREEN}Check the call before it counts.{RESET}", 0.03, pause=2.2)
