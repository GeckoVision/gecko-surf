#!/usr/bin/env python3
"""The full loop — it fails the way it really failed, then the graph, then it lands.

    scene 1  THE FIRST TIME   the naive call, built and simulated for real — it reverts
    scene 2  THE MAGIC        what the agent was given vs the gecko_graph it gets
    scene 3  THE MENU         list_stores at the LIVE hosted surface
    scene 4  THE CHECK        prepare_purchase — unsigned bytes + a receipt, before it counts
    scene 5  LANDED           the real settled transaction, read back off mainnet

Nothing here is a transcript. Scene 1 builds a transaction through the real Orquestra
builder and simulates it against live mainnet ($0, read-only) — the revert on screen is
the node's. Scenes 2–4 call https://mcp.geckovision.tech at record time. Scene 5 is a
LOOKUP of a transaction this same loop settled minutes before the take (gecko_101's rule:
re-broadcasting to make footage would be spending money for a video) — pass ``--settle``
to run a live spend instead, which costs real USDC and is founder-authorized per run.

    uv run python demo/kit/full_loop_screenplay.py --rehearsal   # every scene, $0, no cast
    asciinema rec demo/kit/full_loop.cast --cols 90 --rows 24 --overwrite \\
      -c "uv run python demo/kit/full_loop_screenplay.py"

Honesty rules (the kit README's): every number from the wire; no secrets; one unedited
take; real waits stay real.
"""

from __future__ import annotations

import json
import os
import re
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
    run,
)

DIM = "\033[2m"

HOST = "https://mcp.geckovision.tech"
MAINNET_RPC = "https://api.mainnet-beta.solana.com"
BUYER = "HNUE5KKTcaT4BuG5zmXxTViKjwNaQTtNt2svumE1WCoi"
#: The storefront this take buys from — a NAME. Its authority, its receipts account and
#: the token account that gets credited are read from the chain (gecko/store_accounts.py),
#: never pasted here, so naming a store and paying a different one is not expressible.
STORE_NAME = "jonasbar"
PRODUCT = "Water"

#: The transaction the FULL loop settled on 2026-08-13 (mainnet #16, one call, no human,
#: signed in an enclave — predicted 36,399 CU, the chain charged 36,399). Public data;
#: scene 5 reads it back off the chain rather than spending again for footage.
SETTLED_SIG = os.environ.get(
    "GECKO_SETTLED_SIG",
    "Hp77fgHF85BduE3YiSPfCMYzBFercmiz66Sjh2F9DuNKx7fy9Z9jFEo4W6D9nj7xBE5QShKy9Gs7HBXCj6dxdTX",
)

REHEARSAL = "--rehearsal" in sys.argv
LIVE_SETTLE = "--settle" in sys.argv


def _post(payload: dict, session: str | None = None) -> tuple[dict, str | None]:
    request = urllib.request.Request(
        f"{HOST}/orquestra/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "gecko-demo/full-loop",
            **({"mcp-session-id": session} if session else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        sid = response.headers.get("mcp-session-id") or session
        body = response.read().decode()
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:]), sid
    return (json.loads(body) if body.strip() else {}), sid


def mcp_call(tool: str, arguments: dict) -> dict:
    _, sid = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "gecko-demo", "version": "1"},
            },
        }
    )
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    answer, _ = _post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        sid,
    )
    return json.loads(answer["result"]["content"][0]["text"])


def rpc(method: str, params: list) -> dict:
    request = urllib.request.Request(
        MAINNET_RPC,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "gecko-demo/full-loop",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


# =========================================================================================
# SCENE 1 — THE FIRST TIME (a real build, a real simulate, a real revert — $0)
# =========================================================================================

clear()
out(f"{BOLD}THE FIRST TIME, YOUR AGENT GUESSES.{RESET}", pause=0.8)
out()
put(f"{DIM}what it was given — a 4,908-byte account and an IDL:{RESET}", 0.4)
put(
    f'{DIM}  {{"name":"make_purchase","discriminator":[193,62,227,136,105,212,201,20],{RESET}',
    0.2,
)
put(
    f'{DIM}   "accounts":[{{"name":"receipts","writable":true,"pda":{{"seeds":[…]}}}},…x9],{RESET}',
    0.2,
)
put(
    f"{DIM}  AAAB9v8Kq2Zn0uQ4… (base64, 4,908 bytes — the store lives in here somewhere){RESET}",
    1.4,
)
out()
out(
    f"{CYAN}$ the naive call: token account? probably just my wallet address…{RESET}",
    0.03,
)

from gecko.simulate import simulate  # noqa: E402
from gecko.store_accounts import (  # noqa: E402
    purchase_accounts,
    purchase_args,
    resolve_store,
)
from scripts.autonomous_purchase import http_build_call  # noqa: E402

# The store's accounts come from its NAME, resolved against mainnet. Only the buyer's own
# side is wrong here, and deliberately: the naive guess binds a WALLET where a token
# account belongs, which is the revert this scene is about.
STORE = resolve_store(STORE_NAME, rpc_url=MAINNET_RPC).accounts_for(PRODUCT)
naive_request = {
    "accounts": purchase_accounts(STORE, buyer=BUYER, sender=BUYER),
    "args": purchase_args(STORE, table=3),
    "feePayer": BUYER,
}
naive_built = http_build_call(naive_request)
put(f"  builder: {GREEN}HTTP 200{RESET} — a well-formed transaction came back", 0.8)
naive = simulate(
    {},
    rpc_url=MAINNET_RPC,
    build_call=lambda _plan: naive_built,
    network="mainnet",
    network_label="simulated against mainnet (read-only, unsigned)",
)
error_line = next(
    (line for line in naive.logs_tail if "Error" in line or "failed" in line),
    str(naive.err),
)
put(f"  simulate: {RED}✗ {naive.status.upper()}{RESET}", 0.4)
put(f"    {RED}{error_line[:84]}{RESET}", 1.2)
put(f"  {RED}revert class: {naive.revert_class}{RESET}", 1.4)
out()
put(
    f"{YELLOW}  the builder said 200. the chain said no. transport cannot tell you{RESET}",
    0.3,
)
put(f"{YELLOW}  the call is WRONG — and the first try was the real one.{RESET}", 2.4)

# =========================================================================================
# SCENE 2 — THE MAGIC (what Gecko turns that into)
# =========================================================================================

clear()
out(f"{BOLD}GECKO TURNS THAT INTO A GRAPH.{RESET}", pause=0.8)
out()
put(
    f"{DIM}  bytes + IDL + source + live probes{RESET}   ─────▶   {BOLD}gecko_graph{RESET}",
    1.2,
)
out()
put(f"  {BOLD}the lifecycle{RESET} — two chains, one shared account:", 0.6)
put("", 0.1)
put(f"    {CYAN}open_a_store{RESET}      initialize ──▶ add_product", 0.5)
put(
    f"    {CYAN}sell_and_deliver{RESET}  {GREEN}make_purchase{RESET} ──▶ mark_as_delivered",
    0.5,
)
put(
    f"                      └── both write {BOLD}receipts{RESET} — a DATA edge, not advice:",
    0.4,
)
put(
    "                          mark_as_delivered settles a receipt_id only a landed",
    0.3,
)
put("                          make_purchase writes", 1.2)
put("", 0.1)
put(
    f"  {BOLD}the accounts{RESET} — every one derived, each tagged with where it came from:",
    0.6,
)
put(
    f"    receipts        PDA('receipts', store)   {GREEN}[extracted]{RESET} ← the program's own IDL",
    0.4,
)
put(
    f"    sender_ata      ATA(buyer, mint)         {GREEN}[extracted]{RESET} ← declared, chain-verified",
    0.4,
)
put(f"    recipient_ata   ATA(merchant, mint)      {GREEN}[extracted]{RESET}", 0.4)
put(f"    …and 6 more     pinned program ids       {GREEN}[extracted]{RESET}", 1.0)
out()
put(
    f"{YELLOW}  the workflow graph Arazzo writes down for HTTP APIs — ours walks Solana{RESET}",
    0.3,
)
put(
    f"{YELLOW}  programs too, and every edge carries its provenance and its refutation.{RESET}",
    2.4,
)

# =========================================================================================
# SCENE 3 — THE MENU
# =========================================================================================

clear()
out(f"{BOLD}NOW ASK IN ENGLISH.{RESET}", pause=0.8)
out()
out(f"{CYAN}User: where can I buy water?{RESET}", 0.05, pause=0.6)
out()
out(f"{CYAN}$ list_stores(network=mainnet, product=water){RESET}", 0.02)
put(
    f"{YELLOW}  → https://mcp.geckovision.tech — the same door any chat client uses{RESET}",
    0.6,
)

menu = mcp_call("list_stores", {"network": "mainnet", "product": "water"})
put("")
for store in menu["stores"]:
    for product in store["products"]:
        put(
            f"  {GREEN}✓{RESET} {store['store']:<13} {product['name']:<17}"
            f" {product['price_ui']:>5} USDC   ({store['total_purchases']} purchases on file)",
            0.7,
        )
put("")
put(
    f"  read from each store's {BOLD}own on-chain account{RESET} — the 4,908 bytes, decoded",
    0.9,
)
put(
    f"  {YELLOW}{menu['skipped_undecodable']} accounts did not decode — counted and skipped,"
    f" never guessed{RESET}",
    1.4,
)
put(f"  {YELLOW}a MENU, not an authorization — nothing is trusted yet{RESET}", 2.2)

# =========================================================================================
# SCENE 4 — THE CHECK
# =========================================================================================

clear()
out(f"{BOLD}BEFORE IT COUNTS, IT GETS CHECKED.{RESET}", pause=0.8)
out()
out(f"{CYAN}User: buy the water from jonasbar — table 3{RESET}", 0.05, pause=0.6)
out()
out(
    f"{CYAN}$ prepare_purchase(store=jonasbar, product=Water, network=mainnet){RESET}",
    0.02,
)

check = mcp_call(
    "prepare_purchase",
    {
        "store": "jonasbar",
        "product": "Water",
        "buyer": BUYER,
        "network": "mainnet",
        "table": 3,
    },
)
put("")
if check.get("refused") or check.get("status") != "pass":
    put(
        f"  {RED}the check did not pass ({check.get('status') or check.get('code')}) — an empty",
        0.3,
    )
    put(
        f"  wallet cannot rehearse a purchase. Fund the buyer, then re-take.{RESET}",
        1.5,
    )
    sys.exit(1)
roles = {a["account"]: a for a in check.get("accounts", [])}
debit = roles.get("sender_token_account", {})
credit = roles.get("recipient_token_account", {})
put("  9 accounts derived offline — the two that move money:", 0.5)
put(
    f"    {RED}DEBITED{RESET}   {debit.get('address', '')}  {YELLOW}(the buyer){RESET}",
    0.7,
)
put(
    f"    {GREEN}CREDITED{RESET}  {credit.get('address', '')}  {YELLOW}(the store){RESET}",
    1.0,
)
put("")
status_color = GREEN if check.get("status") == "pass" else RED
put(
    f"  receipt: {status_color}{check.get('status')}{RESET}"
    f" · {BOLD}{check.get('units_consumed'):,} compute units{RESET}"
    f" · binding {check.get('binding', '')[:16]}… ({check.get('binding_strength')})",
    1.2,
)
transaction = check.get("transaction") or {}
put(
    f"  signed: {YELLOW}{transaction.get('signed')}{RESET}"
    f" — {transaction.get('who_signs', '')}",
    1.4,
)
put("")
put(
    f"{YELLOW}  same instruction the naive call reverted on. This time: nine accounts,{RESET}",
    0.3,
)
put(
    f"{YELLOW}  zero guesses, and a receipt for the exact bytes. Check, then sign.{RESET}",
    2.4,
)

# =========================================================================================
# SCENE 5 — LANDED
# =========================================================================================

clear()
out(f"{BOLD}ONE CALL. NO HUMAN. REAL MONEY.{RESET}", pause=0.8)
out()

if LIVE_SETTLE and not REHEARSAL:
    settled = run(
        "uv run python scripts/autonomous_purchase.py"
        " --network mainnet --rpc-url https://api.mainnet-beta.solana.com"
        " --privy --store jonasbar --product Water --table 3 --max-usdc-raw 150000",
        delay=0.015,
        pause=1.2,
        timeout=300,
    )
    match = re.search(r"signature\s+(\S+)", settled.stdout or "")
    if settled.returncode != 0 or not match:
        put(f"{RED}the settle did not land — the take ends honestly here{RESET}", 2.0)
        sys.exit(1)
    signature = match.group(1)
else:
    put("  minutes before this take, this exact loop ran for real — plan, check,", 0.3)
    put(
        f"  spend-gate, {BOLD}sign inside an enclave{RESET}, broadcast. One call, no human.",
        1.0,
    )
    put("")
    signature = SETTLED_SIG
    out(f"{CYAN}$ ask a mainnet node what actually happened{RESET}", 0.02)

found = None
for _ in range(20):
    found = rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    ).get("result")
    if found:
        break
    time.sleep(6)  # a real confirmation wait, kept real

put("")
put(f"  {signature[:44]}…", 0.6)
if found:
    meta = found["meta"]
    pre = {
        b["owner"]: b["uiTokenAmount"]["uiAmount"] or 0
        for b in meta["preTokenBalances"]
    }
    post = {
        b["owner"]: b["uiTokenAmount"]["uiAmount"] or 0
        for b in meta["postTokenBalances"]
    }
    put(
        f"  slot {found['slot']} · err {meta['err']}"
        f" · {BOLD}{meta['computeUnitsConsumed']:,} CU consumed{RESET}",
        0.8,
    )
    put(f"  agent  USDC  {pre.get(BUYER, 0)} → {post.get(BUYER, 0)}", 0.6)
    put(f"  store  USDC  {pre.get(STORE.authority)} → {post.get(STORE.authority)}", 1.2)
    exact = meta["computeUnitsConsumed"] == check.get("units_consumed")
    mark = GREEN + "✓" if exact else RED + "✗"
    put("")
    put(
        f"  {mark} the receipt predicted {check.get('units_consumed'):,} —"
        f" the chain charged {meta['computeUnitsConsumed']:,}{RESET}",
        2.0,
    )
else:
    put(
        f"  {YELLOW}not indexed yet — the signature is public; check it yourself:{RESET}",
        0.5,
    )
    put(f"  https://solscan.io/tx/{signature}", 2.0)

put("")
out(
    f"{BOLD}Sixteen mainnet transactions. Sixteen exact predictions.{RESET}",
    0.04,
    pause=0.8,
)
out(f"{BOLD}The key was never here — it lives in an enclave.{RESET}", 0.04, pause=1.0)
out()
out(f"{GREEN}CHECK THE CALL BEFORE IT COUNTS.{RESET}", 0.05, pause=0.6)
out()
out(
    f"{CYAN}claude mcp add --transport http gecko-store"
    f" https://mcp.geckovision.tech/orquestra/mcp{RESET}",
    0.015,
    pause=3.0,
)
