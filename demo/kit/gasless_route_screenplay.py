#!/usr/bin/env python3
"""A prompt, the execution, the diagram of what ran.

One take, three scenes, on a local copy of mainnet (a surfpool fork on :8899) with a
fee relay (Kora on :8080). Everything after the prompt is the real runner's real output;
the last scene is read back from the trace the run wrote, not typed in.

    export KORA_RPC_URL=http://127.0.0.1:8080 KORA_API_KEY=...   # fork-only, not a secret
    asciinema rec --cols 80 --rows 20 \
        -c "python3 demo/kit/gasless_route_screenplay.py" demo/kit/gasless_route.cast
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put, run  # noqa: E402

DIM = "\033[2m"
WORKTREE = Path(
    os.environ.get(
        "GECKO_DEMO_WORKTREE",
        "/home/nan/PycharmProjects/Gecko/worktrees/surfcall/relay-paid-convert-leg",
    )
)
OUT = Path(os.environ.get("GECKO_DEMO_OUT", "/tmp/gecko-demo"))
OUT.mkdir(parents=True, exist_ok=True)
TRACE = OUT / "run.jsonl"
GRAPH = OUT / "run.html"

# --- scene 1: the prompt -------------------------------------------------------------
clear(0.2)
out(f"{BOLD}A TEXT PROMPT.{RESET}", pause=0.6)
out()
out(f"{DIM}User:{RESET}", delay=0.02)
out(
    f"  {BOLD}buy me an espresso at Gecko Coffee.{RESET}",
    delay=0.05,
)
out(f"  {BOLD}I only have USDG, and no SOL for the fee.{RESET}", delay=0.05, pause=1.2)
out()
put(f"{DIM}  the shop takes USDC. the wallet holds USDG and nothing else.{RESET}", 1.0)
put(f"{DIM}  a wallet with no SOL cannot pay a fee. an agent alone stops here.{RESET}", 1.6)
out()
put(f"{YELLOW}  Gecko reads the shop, converts the money, checks the bill,{RESET}", 0.6)
put(f"{YELLOW}  and a fee relay pays the network so the buyer never needs SOL.{RESET}", 1.8)
out()
put(f"{DIM}  running on a local copy of mainnet (:8899) with a relay (:8080).{RESET}", 2.0)

# --- scene 2: the execution -----------------------------------------------------------
clear()
out(f"{BOLD}THE EXECUTION.{RESET}", pause=0.5)
out()
os.chdir(WORKTREE)
result = run(
    "uv run python scripts/gasless_purchase.py --network fork "
    "--rpc-url http://127.0.0.1:8899 --product Espresso --convert-from USDG "
    f"--broadcast --trace {TRACE} --graph {GRAPH}",
    delay=0.012,
    pause=2.6,
    timeout=400,
)
if result.returncode != 0 or not TRACE.exists():
    put(f"{RED}  the run did not land; the take stops here and says so.{RESET}", 3.0)
    sys.exit(1)

# --- scene 3: the diagram of what ran ------------------------------------------------
clear()
out(f"{BOLD}THE STEPS THAT RAN, READ BACK FROM THE TRACE.{RESET}", pause=0.6)
out()
rows = [json.loads(line) for line in TRACE.read_text().splitlines() if line.strip()]
steps = [r for r in rows if "step" in r]
put(f"{DIM}  {'#':>2}  {'step':<12}{'party':<8}{'time':>9}  outcome{RESET}", 0.3)
put(f"{DIM}  ── ────────────────────────────────────────────────────────────{RESET}", 0.2)
for r in steps:
    ms = r.get("ms")
    when = f"{ms:>7,} ms" if isinstance(ms, int) else f"{'':>10}"
    outcome = str(r.get("outcome") or "")
    if outcome == "ok" and r.get("note"):
        outcome = str(r["note"])
    units = r.get("units")
    if isinstance(units, int):
        outcome = f"{outcome}  {units:,} CU" if outcome else f"{units:,} CU"
    ok = not outcome.startswith("[")
    mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    put(
        f"  {mark}{r.get('seq', 0) + 1:>2} {r.get('step', ''):<12}{r.get('party', ''):<8}"
        f"{when}  {outcome[:34]}",
        0.55,
    )
out()
landed = sum(1 for r in steps if r.get("step") in ("convert", "land") and r.get("outcome") == "ok")
put(f"  {GREEN}✓{RESET} {len(steps)} steps, {landed} landings, the buyer's SOL never moved.", 1.4)
put(f"{DIM}  the same trace, drawn as a sequence diagram: {GRAPH.name}{RESET}", 1.2)
out()
out(f"{BOLD}ANY SHOP ON SOLANA, PAYABLE BY ANY AGENT.{RESET}", pause=0.6)
put(f"{CYAN}  npx @geckovision/gecko{RESET}", 3.0)
time.sleep(0.5)
