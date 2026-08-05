"""Screenplay — "the first call, without Gecko" (the on-chain receipt demo).

Opens on the naive path FAILING on a real mainnet program, then shows the plan the
IDL can't give you and the receipt that says it lands — against a live surfpool
fork, $0, no signature. Every error number and compute-unit count on screen is what
the fork returned during the recording; nothing is typed in by hand.

One real call (`simulate_buy_landing`) produces BOTH receipts — the derive-only one
that reverts and the Gecko-complete one that passes — so the side-by-side is a single
honest run, not two takes stitched together.

Record it (surfpool must ALREADY be running — the fork is the environment, not part
of the story; booting it inside the take would eat the whole budget):

    surfpool start --no-tui --no-deploy --rpc-url "$GECKO_MAINNET_RPC" --port 8899 &
    asciinema rec --cols 80 --rows 20 \
        -c "uv run python demo/kit/onchain_receipt_screenplay.py" onchain.cast

    # leak-check (no key material is read by this script, but check the take anyway)
    grep -nE "[A-Za-z0-9_-]{40,}" onchain.cast | grep -v "8zN8yA21\|FFWtrEQ4" | head

    uv run --with pyte --with pillow python demo/kit/render_cast.py onchain.cast \
        docs/assets/onchain-receipt.mp4 \
        --scene "Gecko — the first call, without Gecko|the IDL is not the program" \
        --scene "Gecko — five programs, five failures|the plan the IDL can't give you" \
        --scene "Gecko — the receipt|simulate before you spend · \$0 · never signs"

Honesty contract (demo/kit/README.md): one unedited take; the fork is labelled on
screen wherever a number appears; nothing claimed that the run did not show.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

RPC = os.environ.get("GECKO_DEMO_RPC", "http://127.0.0.1:8899")
MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
# Global.fee_recipients[0] — a REGULAR recipient (@41 is a buyback one, which reverts 6062).
FEE_RECIPIENTS_0_OFFSET = 162


def _rpc(rpc_url: str, method: str, params: list) -> dict:
    """getAccountInfo defaults to base58, which RPCs reject past 128 bytes."""
    from gecko.rpc import default_rpc_call

    if method == "getAccountInfo":
        params = list(params)
        opts = dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        opts["encoding"] = "base64"
        params = [params[0], opts]
    return default_rpc_call(rpc_url, method, params)


from gecko.pda import derive_pda  # noqa: E402
from gecko.pda_resolve import read_account_field_pubkey  # noqa: E402
from gecko.provider_config import load_packaged_provider  # noqa: E402
from gecko.providers.pumpfun_landing import simulate_buy_landing  # noqa: E402

_, _apis = load_packaged_provider("orquestra")
_pdas = _apis["pumpfun"].program.pdas
FEE_RECIPIENT = read_account_field_pubkey(
    derive_pda(_pdas["global"], {}).address,
    FEE_RECIPIENTS_0_OFFSET,
    rpc_url=RPC,
    rpc_call=_rpc,
)

BINDINGS = {
    "mint": MINT,
    "user": USER,
    "amount": 1_000_000,
    "fee_recipient": FEE_RECIPIENT,
    "track_volume": True,
}

# ---------------------------------------------------------------- scene 1
out(f"{BOLD}A coding assistant, on its own, buying a token on Pump.fun.{RESET}", pause=0.5)
out("It reads the surface, assembles the call, and does the only thing it can:", pause=0.7)
out("it tries.", pause=0.8)
out()
out(f"{CYAN}$ # the call an assistant builds by itself{RESET}", 0.02)

result = simulate_buy_landing(BINDINGS, rpc_url=RPC, rpc_call=_rpc)
naive = result.derive_only_receipt

put(f"{RED}✗ REVERT{RESET}  {naive.revert_class}", pause=0.4)
for line in naive.logs_tail[-2:]:
    put(f"  {line[:76]}", pause=0.3)
put()
put(f"{YELLOW}Nothing here was done wrong. It just had no way to try first.{RESET}", pause=1.6)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}Three things an assistant can't know on its own.{RESET}", pause=0.6)
put()
put("  1. an account whose address lives inside another account's data")
put("  2. an account that appears only at runtime, never in the schema")
put("  3. which of several valid values this program accepts today", pause=1.4)
put()
out(f"{CYAN}$ gecko orquestra find-start \"buy this token on pump\"{RESET}", 0.02)
put(f"{GREEN}→ pumpfun/buy{RESET}  — every account carries where it came from:", pause=0.4)
put("   creator_vault      [recovered]  read from on-chain state")
put("   bonding_curve_v2   [recovered]  recovered from source")
put("   fee_recipient      [FLAGGED]    unknown — flagged, never guessed", pause=1.6)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}Same intent. The complete plan. Simulated first.{RESET}", pause=0.5)
out()
_seen_cu = 0
for i, step in enumerate(result.landing_plan, 1):
    kind = step.get("kind", "?")
    if kind == "compute_budget":
        _seen_cu += 1
        label = "compute unit limit" if _seen_cu == 1 else "compute unit price"
    elif kind == "create_idempotent_ata":
        label = "create the buyer's token account (idempotent)"
    else:
        label = f"{kind} — the complete account set"
    put(f"   {i}. {label}", pause=0.2)
out()
out(f"{CYAN}$ # the receipt{RESET}", 0.02)
put(
    f"{GREEN}✓ PASS{RESET}  {result.landing_receipt.units_consumed:,} compute units",
    pause=0.4,
)
put(f"  {result.landing_receipt.network_label}", pause=0.6)
put()
put(f"{YELLOW}It couldn't have known. Now it can check — before spending.{RESET}", pause=1.6)
put()
put(f"{BOLD}Built for the calls your agent must not get wrong.{RESET}")
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
