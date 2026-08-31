#!/usr/bin/env python3
"""WITHOUT / WITH Gecko — the same order, twice, ending on a mainnet receipt.

    scene 1  WITHOUT GECKO   the naive build (accounts guessed from the IDL), simulated
                             against live mainnet — the real revert, unedited
    scene 2  WITH GECKO      the store resolved from its NAME, three real purchases
                             signed and landed: 2 Espresso + 1 Sparkling water
    scene 3  THE RECEIPT     the last transaction read back off the chain by signature

The order: buy 2 espressos and 1 sparkling water at geckocoffee. Scene 2 SPENDS REAL
MONEY when ``--settle`` is passed (0.25 USDC + fees, from the demo buyer keypair).
Without it, the settle step is narrated but skipped and scene 3 reads back a purchase
this loop already settled — the numbers on screen still come off the chain.

    GECKO_RPC_URL=... uv run python demo/kit/with_without_purchase_screenplay.py --rehearsal
    GECKO_RPC_URL=... asciinema rec demo/kit/with_without.cast --cols 80 --rows 20 \\
        --overwrite -c "uv run python demo/kit/with_without_purchase_screenplay.py --settle"
    agg --font-size 16 demo/kit/with_without.cast demo/kit/with_without.gif

Honesty rules (demo/kit/README.md, law): every number off the wire during the take; the
failure in scene 1 is a REAL simulation revert (the exact mistake upstream's own CLI
made: accounts resolved for one store, argument naming another); one unedited take; the
RPC URL carries an API key and is NEVER printed — the displayed command shows $GECKO_RPC_URL.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

DIM = "\033[38;5;245m"
BLUE = "\033[38;5;33m"

REHEARSAL = "--rehearsal" in sys.argv
LIVE_SETTLE = "--settle" in sys.argv

STORE = "geckocoffee"
ORDER = ["Espresso", "Espresso", "Sparkling water"]
BUYER_KEYPAIR = Path.home() / ".gecko" / "wallets" / "demo-buyer.json"

#: A purchase this loop already settled — scene 3's fallback when --settle is absent.
SETTLED_SIG = os.environ.get(
    "GECKO_SETTLED_SIG",
    "3zTRSSJqe9HDKi7vf5fW8jW8oMjfpDrhwBRYynpPC9jVuUfoBhf4xqYBS9Prb5EFGyXZjmVU6TSAJtA32VBDviH",
)

RPC = os.environ.get("GECKO_RPC_URL", "")
if not RPC:
    sys.exit("set GECKO_RPC_URL (it carries an API key and is never printed)")
RPC_SHOWN = "$GECKO_RPC_URL"


def rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        RPC, body.encode(), {"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def tab(active: str) -> None:
    """The landing page's two-tab header, in cells."""
    without = (
        f"{BOLD}\033[47;30m WITHOUT GECKO \033[0m"
        if active == "without"
        else f"{DIM} WITHOUT GECKO {RESET}"
    )
    with_ = (
        f"{BOLD}\033[44;97m WITH GECKO \033[0m"
        if active == "with"
        else f"{DIM} WITH GECKO {RESET}"
    )
    put(f" {without}  {with_}", 0.5)
    put("")


# ─────────────────────────────────────────────────────────────────────────────
# scene 1 — WITHOUT GECKO: the naive build, simulated for real
# ─────────────────────────────────────────────────────────────────────────────
clear()
tab("without")
out(
    f"{CYAN}$ agent: buy 2 espressos + 1 sparkling water at {STORE}{RESET}",
    0.03,
    pause=0.8,
)
put("")
put("  reads the IDL. derives the receipts account from what it names.", 0.5)
put(f"  {DIM}(the exact accounts the program's own CLI derives){RESET}", 0.8)
put("")

from gecko.autonomous_purchase import PurchasePlan  # noqa: E402
from gecko.networks import coerce_network  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from gecko.simulate import simulate  # noqa: E402
from gecko.store_accounts import purchase_accounts, purchase_args, resolve_store  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from autonomous_purchase import http_build_call  # noqa: E402
from solders.keypair import Keypair  # noqa: E402

buyer_pubkey = str(
    Keypair.from_bytes(bytes(json.loads(BUYER_KEYPAIR.read_text()))).pubkey()
)

right = resolve_store(STORE, rpc_url=RPC, rpc_call=default_rpc_call)
wrong = resolve_store("jonasbar", rpc_url=RPC, rpc_call=default_rpc_call).accounts_for(
    "Water"
)
naive_plan = PurchasePlan(
    api_id="let_me_buy",
    instruction="make_purchase",
    # The naive resolution: account set from the CLI's default store, `store_name`
    # argument naming the store the user asked for. Both look valid alone.
    accounts=purchase_accounts(
        wrong, buyer=buyer_pubkey, recipient=wrong.token_account
    ),
    args=purchase_args(right.accounts_for("Espresso"), table=11),
    fee_payer=buyer_pubkey,
)

put(f"  {DIM}simulating against live mainnet (unsigned, $0)…{RESET}", 0.2)
sim_error = ""
try:
    naive_receipt = simulate(
        naive_plan.build_request(),
        rpc_url=RPC,
        rpc_call=default_rpc_call,
        build_call=http_build_call,
        network=coerce_network("mainnet"),
        network_label="simulated against LIVE mainnet (read-only, unsigned)",
    )
    if naive_receipt.status != "pass":
        sim_error = (
            json.dumps(naive_receipt.err) if naive_receipt.err else naive_receipt.status
        )
        if naive_receipt.revert_class:
            sim_error += f" [{naive_receipt.revert_class}]"
except Exception as exc:  # the revert IS the demo — show it, unedited
    sim_error = str(exc)

put("")
if sim_error:
    shown = sim_error if len(sim_error) <= 74 else sim_error[:74] + "…"
    put(f"  {RED}✗ simulation reverted{RESET}", 0.4)
    put(f"    {RED}{shown}{RESET}", 0.8)
    put("")
    put(
        f"  {YELLOW}the seed the IDL names is not the seed the program checks{RESET}",
        0.6,
    )
    put(f"{RED}  -> status: fail. 0 of 3 purchases landed. funds untouched{RESET}", 1.8)
else:
    put(
        f"  {YELLOW}the naive build passed this time — recording continues honestly{RESET}",
        1.8,
    )

# ─────────────────────────────────────────────────────────────────────────────
# scene 2 — WITH GECKO: resolve by name, land three purchases
# ─────────────────────────────────────────────────────────────────────────────
clear()
tab("with")
out(
    f"{CYAN}$ agent: buy 2 espressos + 1 sparkling water at {STORE}{RESET}",
    0.03,
    pause=0.6,
)
put("")
put(f"  {GREEN}✓{RESET} name resolved on-chain: menu, prices, receipts, authority", 0.5)
for product in right.products:
    if product.name in ORDER:
        count = ORDER.count(product.name)
        put(f"    {product.name:<18} {product.price_ui:>6} USDC  × {count}", 0.4)
put("")


def run_driver(item: str) -> tuple[bool, str, str, str, str]:
    """One driver run. Returns (settled, signature, predicted, consumed, reason).

    Only a run whose stdout says SETTLED counts: the driver also prints a
    `signature` line on its UNCONFIRMED path, and an unconfirmed broadcast is an
    UNKNOWN outcome, never a landed purchase.
    """
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
            "--keypair",
            str(BUYER_KEYPAIR),
            "--store",
            STORE,
            "--product",
            item,
            "--table",
            "7",
            # The demo buyer's own rolling spend ledger. The default ledger is
            # machine-wide and already counts today's development transactions;
            # the cap protects THIS wallet's spend, so it gets its own file.
            "--ledger",
            str(Path.home() / ".gecko" / "spend-ledger-demo-buyer.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    settled = any(line.strip() == "SETTLED" for line in completed.stdout.splitlines())
    sig = predicted = consumed = reason = ""
    for line in completed.stdout.splitlines():
        if m := re.match(r"\s*signature\s+(\S+)", line):
            sig = m.group(1)
        elif m := re.match(r"\s*predicted\s+(\S+)", line):
            predicted = m.group(1)
        elif m := re.match(r"\s*consumed\s+(\S+)", line):
            consumed = m.group(1)
        elif m := re.match(r"\s*reason\s+(.+)", line):
            reason = m.group(1).strip()
    return settled, sig, predicted, consumed, reason


def landed_late(signature: str) -> bool:
    """Whether an UNCONFIRMED broadcast landed after the driver stopped watching.

    A broadcast that lands late still spends, so it MUST be counted before any
    retry — retrying an actually-landed purchase would buy the item twice.
    """
    for _ in range(15):
        statuses = rpc("getSignatureStatuses", [[signature]])["result"]["value"]
        status = statuses[0]
        if status is not None and status.get("err") is None:
            return True
        time.sleep(2.0)
    return False


signatures: list[str] = []
if LIVE_SETTLE and not REHEARSAL:
    for item in ORDER:
        put(f"  {DIM}{item}: simulate → verify → sign → land…{RESET}", 0.1)
        settled = sig = predicted = consumed = reason = ""
        for attempt in (1, 2):
            settled, sig, predicted, consumed, reason = run_driver(item)
            if settled:
                break
            if sig and landed_late(sig):
                settled = True  # it spent; it counts, whatever the driver saw
                break
            if not sig:
                # No signature means a REFUSAL, not a lost broadcast. A refusal
                # is an answer; retrying it would just ask the same question.
                break
            if attempt == 1:
                put(
                    f"    {YELLOW}broadcast expired unspent — retrying once{RESET}", 0.2
                )
        if settled and sig:
            signatures.append(sig)
            cu = (
                f"predicted {BOLD}{predicted} CU{RESET} → consumed {BOLD}{consumed} CU{RESET}"
                if consumed
                else "landed late — read in scene 3"
            )
            put(f"  {GREEN}✓ {item:<15}{RESET} {cu}", 0.2)
            put(f"    {DIM}{sig[:44]}…{RESET}", 0.5)
        elif not sig:
            put(
                f"  {RED}✗ {item} REFUSED before signing — the gate's words:{RESET}",
                0.3,
            )
            put(f"    {RED}{reason[:74]}{RESET}", 0.5)
        else:
            put(
                f"  {RED}✗ {item} did not settle — twice. shown, not hidden{RESET}", 0.4
            )
    put("")
    landed = len(signatures)
    colour = BLUE if landed == len(ORDER) else YELLOW
    verdict = "pass" if landed == len(ORDER) else "partial"
    put(
        f"{colour}  -> status: {verdict}. {landed} of {len(ORDER)} landed on mainnet{RESET}",
        1.2,
    )
else:
    put(
        f"  {DIM}(rehearsal: settle skipped — scene 3 reads a prior settled purchase){RESET}",
        1.0,
    )

# ─────────────────────────────────────────────────────────────────────────────
# scene 3 — THE RECEIPT, read back off the chain
# ─────────────────────────────────────────────────────────────────────────────
clear()
out(f"{BOLD}The receipt — from mainnet, by signature{RESET}", 0.03, pause=0.5)
put("")

# The FIRST landed purchase: by now it has been on chain for the whole of scene 2,
# so getTransaction serves it without the indexing lag the newest signature has.
final_sig = signatures[0] if signatures else SETTLED_SIG
detail = None
for attempt in range(75):
    detail = rpc(
        "getTransaction",
        [final_sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
    )["result"]
    if detail is not None:
        break
    if attempt == 0:
        put(f"  {DIM}waiting for the chain to index it…{RESET}", 0.0)
    time.sleep(2.0)
if detail is None:
    put(
        f"  {RED}{final_sig} did not appear within 60s — check before re-running{RESET}",
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

put(f"  {GREEN}SETTLED{RESET}  slot {detail['slot']}   error {meta['err']}", 0.5)
put(f"  {final_sig[:44]}…", 0.5)
put("")
put(
    f"    consumed      {BOLD}{meta['computeUnitsConsumed']} CU{RESET}  {DIM}— read off the chain{RESET}",
    0.6,
)
put(
    f"    buyer  USDC   {pre.get(buyer_pubkey, '?')} → {post.get(buyer_pubkey, '?')}",
    0.4,
)
put(
    f"    store  USDC   {pre.get(right.authority, '?')} → {post.get(right.authority, '?')}",
    0.8,
)
if len(signatures) > 1:
    put("")
    put(f"    {DIM}all {len(signatures)} signatures, checkable by anyone:{RESET}", 0.3)
    for sig in signatures:
        put(f"    {DIM}solscan.io/tx/{sig[:30]}…{RESET}", 0.3)
put("")
out(f"{GREEN}Check the call before it counts.{RESET}", 0.03, pause=2.2)
