"""Screenplay — "try the call before you make it" (the CLI cut).

One command, typed by a person, three answers back: which call, what it needs and where
each account came from, and whether it lands. The narration is minimal on purpose — the
terminal output IS the demo, and every line of it comes off the wire during the take.

Record it (surfpool must ALREADY be running — the fork is the environment, not the story):

    surfpool start --no-tui --no-deploy --rpc-url "$GECKO_MAINNET_RPC" --port 8899 &
    asciinema rec --cols 92 --rows 26 \
        -c "uv run python demo/kit/prove_screenplay.py" prove.cast

    grep -nE "[A-Za-z0-9_-]{40,}" prove.cast | head    # leak-check the take

    uv run --with pyte --with pillow python demo/kit/render_cast.py prove.cast \
        docs/assets/prove.mp4 \
        --brand "GECKO  •  TRY THE CALL BEFORE YOU MAKE IT" \
        --scene "Gecko — one question|the agent has to pick the right call" \
        --scene "Gecko — the choice, and what it rejected|selection, not luck" \
        --scene "Gecko — the receipt|\$0 · unsigned · never mainnet"

Honesty contract (demo/kit/README.md): one unedited take; the fork is labelled on screen
wherever a number appears; nothing claimed that the run did not show.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from gecko.cli import main as gecko  # noqa: E402
from screenplay import BOLD, CYAN, GREEN, RESET, YELLOW, clear, out, put  # noqa: E402

HYUSD = "5YMkXAYccHSGnHn9nob9xEvv6Pvka9DZWH7nTbotTu9E"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
HOLDER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"


def run(argv: list[str]) -> list[str]:
    """Run the real CLI in-process and hand back its lines, so the take shows the
    command's own output rather than a re-typed imitation of it."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        gecko(argv)
    return buffer.getvalue().rstrip("\n").split("\n")


def echo(lines: list[str], pause: float = 0.16) -> None:
    for line in lines:
        put(line, pause=pause)


# ---------------------------------------------------------------- scene 1
out(f"{BOLD}An agent has to make one call on Solana.{RESET}", pause=0.5)
out("Not 'call an API' — pick the right instruction, on the right program,", pause=0.6)
out(
    "with every account it needs. Getting it wrong costs a real transaction.", pause=0.9
)
out()
out(f'{CYAN}$ gecko prove "get me out of hyUSD into USDC"{RESET}', 0.03)

echo(run(["prove", "get me out of hyUSD into USDC"]), pause=0.25)
put()
put(f"{YELLOW}It refused. Nothing named a program or an action —{RESET}", pause=0.6)
put(
    f"{YELLOW}so it will not hand back a plan it cannot stand behind.{RESET}", pause=1.6
)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}Say what you actually want.{RESET}", pause=0.5)
out()
out(f'{CYAN}$ gecko prove "route a swap out of hyUSD" --bind …{RESET}', 0.03)
put()

_lines = run(
    [
        "prove",
        "route a swap out of hyUSD",
        "--bind",
        f"input_mint={HYUSD}",
        "--bind",
        f"output_mint={USDC}",
        "--bind",
        "amount=10000000",
        "--bind",
        f"user={HOLDER}",
    ]
)

# scene 2 = the routing half (up to the account breakdown), scene 3 = the receipt
_split = next(
    (i for i, line in enumerate(_lines) if "the call needs" in line), len(_lines)
)
echo(_lines[:_split], pause=0.22)
put()
put(
    f"{YELLOW}That is the mechanism: the field it searched, the score,{RESET}",
    pause=0.6,
)
put(
    f"{YELLOW}the terms each candidate matched, and what got demoted.{RESET}", pause=1.6
)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}Now the part no schema can answer.{RESET}", pause=0.6)
out()
echo(_lines[_split:], pause=0.3)
put()
put("  16 of those accounts are in no IDL — they are the legs of the route", pause=0.5)
put("  an aggregator picked a second ago. A different surface knew them.", pause=1.2)
put()
put(
    f"{GREEN}Simulated first. On a fork. Nothing signed, nothing spent.{RESET}",
    pause=1.0,
)
put()
put(f"{BOLD}Try the call before you make it.{RESET}")
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
