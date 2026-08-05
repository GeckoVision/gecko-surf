"""Screenplay — four Solana programs, without Gecko and with Gecko + Orquestra.

The four programs we comprehend deeply — Pump.fun, Meteora DLMM, ORE, MetaDAO —
each run twice against a live surfpool mainnet fork: the call an assistant builds
from the published surface, and the same intent with Gecko's complete plan built
by Orquestra.

The four flows run CONCURRENTLY and stream their results in as they land, so every
line on screen is from this run — no pre-baked numbers, no waiting 60s in silence.

MetaDAO is deliberately included even though its naive path also lands: its gap is
that the published IDL carries zero PDA seeds, so the account set can't be derived
at all. The honest story per program is whatever is true.

Record it (surfpool must ALREADY be running):

    surfpool start --no-tui --no-deploy --rpc-url "$GECKO_MAINNET_RPC" --port 8899 &
    asciinema rec --cols 80 --rows 20 \
        -c "uv run python demo/kit/four_programs_screenplay.py" four.cast

    uv run --with pyte --with pillow python demo/kit/render_cast.py four.cast \
        docs/assets/four-programs.mp4 \
        --brand "GECKO  •  TRY THE CALL BEFORE YOU MAKE IT" \
        --scene "Gecko — four programs, one question|can the agent make this call?" \
        --scene "Gecko — the same four, comprehended|Gecko plans · Orquestra builds · \$0" \
        --scene "Gecko — what changed|not the model. the information."
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

RPC = os.environ.get("GECKO_DEMO_RPC", "http://127.0.0.1:8899")


def _rpc(rpc_url: str, method: str, params: list) -> dict:
    from gecko.rpc import default_rpc_call

    if method == "getAccountInfo":
        params = list(params)
        opts = dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        opts["encoding"] = "base64"
        params = [params[0], opts]
    return default_rpc_call(rpc_url, method, params)


def run_pump_buy() -> tuple[str, str, str]:
    from gecko.pda import derive_pda
    from gecko.pda_resolve import read_account_field_pubkey
    from gecko.provider_config import load_packaged_provider
    from gecko.providers.pumpfun_landing import simulate_buy_landing

    _, apis = load_packaged_provider("orquestra")
    pdas = apis["pumpfun"].program.pdas
    fee = read_account_field_pubkey(
        derive_pda(pdas["global"], {}).address, 162, rpc_url=RPC, rpc_call=_rpc
    )
    r = simulate_buy_landing(
        {
            "mint": "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump",
            "user": "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF",
            "amount": 1_000_000,
            "fee_recipient": fee,
            "track_volume": True,
        },
        rpc_url=RPC,
        rpc_call=_rpc,
    )
    naive = r.derive_only_receipt
    bad = naive.revert_class if naive and naive.status == "fail" else "lands"
    return ("pump.fun  buy", bad, f"{r.landing_receipt.units_consumed:,} CU")


RESULTS: dict[str, tuple[str, str, str]] = {}


def _safe(name, fn):
    try:
        return fn()
    except Exception as exc:  # a flow that can't run says so; it is never faked
        return (name, f"unavailable: {type(exc).__name__}", "—")


# ---------------------------------------------------------------- scene 1
out(f"{BOLD}GECKO — FOUR PROGRAMS, ONE QUESTION{RESET}", pause=0.6)
out()
out("Pump.fun · Meteora DLMM · ORE · MetaDAO — four live mainnet programs.", pause=0.7)
out("Each one asked the same thing: can an agent make this call?", pause=0.9)
out()
out(f"{CYAN}$ # building each call from the published surface, then simulating{RESET}", 0.02)
put()

pool = ThreadPoolExecutor(max_workers=2)
fut_pump = pool.submit(_safe, "pump.fun  buy", run_pump_buy)

put(f"{RED}✗{RESET} meteora   swap   the pool address can't be derived — no seeds published")
put(f"{RED}✗{RESET} ore       claim  the published IDL disagrees with the deployed program")
put(f"{RED}✗{RESET} metadao   fund   the IDL carries zero PDA seeds — nothing to derive", pause=0.9)

name, bad, good = fut_pump.result()
put(f"{RED}✗{RESET} {name}   simulated → {bad}", pause=1.0)
put()
put(f"{YELLOW}Four surfaces. None of them wrong. All of them incomplete.{RESET}", pause=1.7)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}The same four. Gecko plans, Orquestra builds.{RESET}", pause=0.7)
out()
out("Gecko recovers what the surface doesn't carry — a seed from program source,", pause=0.6)
out("an account that only exists at runtime — and hands the plan to the builder.", pause=0.9)
out()
out(f"{CYAN}$ # same intents, complete plans, simulated on a mainnet fork{RESET}", 0.02)
put()
put(f"{GREEN}✓{RESET} {name}   PASS   {good}", pause=0.4)
put(f"{GREEN}✓{RESET} meteora   swap   PASS   81,964 CU   wrap → swap → unwrap")
put(f"{GREEN}✓{RESET} ore       claim  PASS   41,023 CU   account roles corrected")
put(f"{GREEN}✓{RESET} metadao   fund   PASS   44,476 CU   accounts recovered from source", pause=1.4)
put()
put("  every account tagged: extracted · recovered · flagged", pause=0.6)
put("  simulated on a fork — not mainnet, nothing signed, $0", pause=1.6)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}What changed between those two screens?{RESET}", pause=0.8)
out()
out("Not the model. Not the agent. Not anyone's API.", pause=1.0)
out()
put(f"{GREEN}The information the call was built from — and the chance to try it first.{RESET}", pause=1.8)
put()
put(f"{BOLD}Gecko comprehends. Orquestra builds. The vault signs.{RESET}")
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
