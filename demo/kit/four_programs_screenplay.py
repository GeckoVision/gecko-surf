"""Screenplay — four Solana programs, without Gecko and with Gecko + Orquestra.

Pump.fun, Meteora DLMM, ORE and MetaDAO, each run twice against a live surfpool
mainnet fork: the call built from the published surface, and the same intent with
Gecko's complete plan built by Orquestra.

Every line comes from `four_programs_sweep.sweep()` running during the take — the four
flows go concurrently and stream in as they land. Nothing is hardcoded. MetaDAO is kept
in even though its naive path also lands: its gap is that the published IDL carries no
seeds at all, so the accounts can't be derived. The true story per program is whatever
is true.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from four_programs_sweep import sweep  # noqa: E402
from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

# ---------------------------------------------------------------- scene 1
out(f"{BOLD}GECKO — FOUR PROGRAMS, ONE QUESTION{RESET}", pause=0.5)
out()
out("Pump.fun · Meteora DLMM · ORE · MetaDAO — four live mainnet programs.", pause=0.7)
out("Each asked the same thing: can an agent make this call?", pause=0.9)
out()
out(
    f"{CYAN}$ # building each call from the published surface, then simulating{RESET}",
    0.02,
)
put()

ROWS = []


def _show(row):
    label, naive, _ = row
    ROWS.append(row)
    mark = f"{RED}✗{RESET}" if naive != "also lands" else f"{YELLOW}~{RESET}"
    put(f"  {mark} {label}   {naive}")


sweep(on_result=_show)
put(pause=1.0)
put(
    f"{YELLOW}Four surfaces. None of them wrong. All of them incomplete.{RESET}",
    pause=1.7,
)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}The same four. Gecko plans, Orquestra builds.{RESET}", pause=0.6)
out()
out(
    "Gecko recovers what the surface doesn't carry — a seed from program source,",
    pause=0.6,
)
out(
    "an account that exists only at runtime — and hands the plan to the builder.",
    pause=0.9,
)
put()
for label, _, good in ROWS:
    put(f"  {GREEN}✓{RESET} {label}   {good}", pause=0.3)
put()
put("  every account tagged: extracted · recovered · flagged", pause=0.5)
put("  simulated on a fork — not mainnet, nothing signed, $0", pause=1.7)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}What changed between those two screens?{RESET}", pause=0.8)
out()
out("Not the model. Not the agent. Not anyone's API.", pause=1.0)
out()
put(
    f"{GREEN}The information the call was built from — and the chance to try it first.{RESET}",
    pause=1.8,
)
put()
put(f"{BOLD}Gecko comprehends. Orquestra builds. The vault signs.{RESET}")
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
