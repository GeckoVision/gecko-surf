#!/usr/bin/env python3
"""Gecko 101 — three tiers, each strictly stronger than the last, every command real.

    tier 1  routing            no chain touched
    tier 2  live mainnet       read-only simulateTransaction, real state, $0
    tier 3  a landed tx        already broadcast, already public

Nothing here is a transcript. Each `$` line runs through ``screenplay.run`` and what
appears beneath it is what the process wrote during the take. A command that fails on
camera is the take telling us something true.

Tier 3 is a LOOKUP, not a broadcast. This file never signs and never sends — the founder
does that, separately, and re-broadcasting to make footage would be spending money for a
video.

    uv run python demo/kit/orquestra_sweep.py          # tier 1 data, live
    uv run python demo/kit/gecko_101_screenplay.py     # dry run, read every line

    asciinema rec demo/kit/gecko_101.cast --cols 90 --rows 24 --overwrite \\
      -c "uv run python demo/kit/gecko_101_screenplay.py"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screenplay import BOLD, CYAN, GREEN, RESET, YELLOW, clear, out, put, run  # noqa: E402

MAINNET = "https://api.mainnet-beta.solana.com"
SWEEP = Path(__file__).with_name("orquestra_sweep.json")
SIGNATURE = (
    "5cjBs5VE8WVVctG2EoUkYiRkW92sXkoT4YsNxszWC9CE3sK7tri"
    "TJ5vnY6TrcQ2BRPYUtWsd3LtTnyieUfn8Hw2Y"
)


def scene_problem() -> None:
    clear()
    out(f"{BOLD}An agent's first call is the real one.{RESET}", pause=1.2)
    put()
    out("A wrong call that errors is cheap. You find out.", pause=1.0)
    put()
    out(f"{YELLOW}The expensive one is quiet.{RESET}", pause=0.8)
    out("Well-formed. Accepted. Nothing throws. The world is now wrong.", pause=1.8)


def scene_tier1_routing() -> None:
    """Refusals first. A tool that answers everything tells you nothing about when to
    trust it, so the four it declines carry more weight than the six it routes."""
    clear()
    out(f"{BOLD}Tier 1 — ten questions, one live server.{RESET}", pause=1.0)
    put(f"{CYAN}mcp.geckovision.tech/orquestra{RESET}  ·  no chain touched", pause=1.4)
    put()
    run("uv run python demo/kit/orquestra_sweep.py", timeout=420, pause=3.0)
    put()
    out("Six routed. Four refused — nothing in them names a program.", pause=1.6)
    out(
        f"{GREEN}Every account tagged: extracted, recovered, or flagged.{RESET}",
        pause=2.2,
    )


def scene_tier2_mainnet() -> None:
    """The tier that costs nothing and proves the most: real chain, real state, real
    compute units, and the naive path failing beside ours in the same run."""
    clear()
    out(f"{BOLD}Tier 2 — the same calls, against live mainnet.{RESET}", pause=1.0)
    put(f"{CYAN}{MAINNET}{RESET}", pause=1.0)
    put("read-only · nothing signed · $0", pause=1.4)
    put()
    run(
        "uv run python demo/kit/four_programs_sweep.py",
        timeout=420,
        pause=3.0,
    )
    put()
    out("Left column: the published surface, called naively. It reverts.", pause=1.6)
    out("Right column: the same call with the seeds recovered from source.", pause=1.6)
    put()
    out(
        f"{YELLOW}Not the builder's fault — the seed is absent from the IDL.{RESET}",
        pause=1.4,
    )
    out(
        f"{GREEN}Gecko recovers it. Orquestra builds. Nothing here signs.{RESET}",
        pause=2.4,
    )


def scene_tier3_landed() -> None:
    clear()
    out(f"{BOLD}Tier 3 — one that already landed.{RESET}", pause=1.2)
    put()
    # A `curl` would be the obvious choice and it does not survive shlex.split — the
    # JSON body arrives mangled and the node answers "Parse error". Found in the dry
    # run. Same request, same public endpoint, legible on camera.
    run(f"uv run python demo/kit/verify_tx.py {SIGNATURE}", timeout=90, pause=1.0)
    put()
    out("0.1 USDC. A real bar. A real bottle of water.", pause=1.2)
    put()
    out(f"the receipt predicted  {BOLD}36,508{RESET} compute units", pause=0.9)
    out(f"the chain consumed     {BOLD}36,508{RESET}", pause=2.0)
    put()
    out(
        f"{GREEN}Gecko held no key and broadcast nothing. It never does.{RESET}",
        pause=2.2,
    )


def scene_close() -> None:
    clear()
    out(f"{BOLD}Gecko{RESET}", pause=0.6)
    put()
    out("Your agent tries the call before it makes it.", pause=1.5)
    put()
    put(f"{CYAN}npx @geckovision/gecko@latest{RESET}", pause=0.9)
    put(f"{CYAN}docs.geckovision.tech{RESET}", pause=2.5)


def preflight() -> None:
    """Fail loudly BEFORE a take rather than halfway through one.

    Recording over a stale sweep would put yesterday's numbers on camera under a claim
    that they are live — the one thing this kit's honesty rules forbid outright.
    """
    if not SWEEP.exists():
        sys.exit("run demo/kit/orquestra_sweep.py first — tier 1 needs live data")
    rows = json.loads(SWEEP.read_text("utf-8"))
    if any(str(r.get("routed", "")).startswith("ERROR") for r in rows):
        sys.exit("the last sweep had transport errors; re-run it before recording")


def main() -> int:
    preflight()
    for scene in (
        scene_problem,
        scene_tier1_routing,
        scene_tier2_mainnet,
        scene_tier3_landed,
        scene_close,
    ):
        scene()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
