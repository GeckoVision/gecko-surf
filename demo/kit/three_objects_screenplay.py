"""Screenplay — "same question, three objects".

One question, asked three times, of three different things:

    what does this depend on, and where did each part come from?

  1. OUR OWN CODE      — a code knowledge graph (Graphify, on-device AST)
  2. TWO HTTP APIs     — gecko export-arazzo, a cross-API plan
  3. AN ON-CHAIN PROGRAM — gecko orquestra find-start, a derive plan

Three objects, one shape: a graph, provenance on every edge, and an explicit floor —
the thing each one refuses to guess. The floor is the point. Object 1's floor is our
own doing (a Protocol the AST cannot follow); object 2's is a join no spec declares;
object 3's is an account no IDL names.

Every command executes for real. Nothing here signs, spends, or needs a key.

PREREQUISITE (the code graph is built once, on-device, no API key):

    uv tool install graphifyy
    graphify . --code-only

Record it:

    asciinema rec --cols 92 --rows 30 \
        -c "uv run python demo/kit/three_objects_screenplay.py" three_objects.cast

    grep -nE "[A-Za-z0-9_-]{40,}" three_objects.cast | head   # leak-check the take

    uv run --with pyte --with pillow python demo/kit/render_cast.py three_objects.cast \
        docs/assets/three-objects.mp4 \
        --brand "GECKO  •  SAME QUESTION, THREE OBJECTS" \
        --scene "One question|what does this depend on, and where did it come from" \
        --scene "Object 1 — our own code|the seam the AST cannot see" \
        --scene "Object 2 — two HTTP APIs|the join no spec declares" \
        --scene "Object 3 — an on-chain program|the account no IDL names" \
        --scene "Three objects, one shape|a graph, provenance, and an honest floor"

Honesty contract (demo/kit/README.md): one unedited take; nothing claimed that the run
did not show. If a command fails on camera, the take is wrong or the product is — both
are worth knowing before the video ships.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from screenplay import (  # noqa: E402
    BOLD,
    CYAN,
    GREEN,
    RESET,
    YELLOW,
    clear,
    out,
    put,
    run,
)

GRAPH = REPO / "graphify-out" / "graph.json"
PEGANA = "tests/fixtures/pegana_p0_openapi.json"
BIRDEYE = "examples/birdeye_demo/spec/birdeye_openapi.json"

QUESTION = "what does this depend on, and where did each part come from?"


def scene_one_question() -> None:
    clear()
    out(f"{BOLD}One question.{RESET}", pause=0.6)
    out()
    out(f"  {YELLOW}{QUESTION}{RESET}", pause=1.2)
    out()
    out("Ask it of three different things.", pause=0.8)
    out()
    put(f"  1. {BOLD}our own code{RESET}          a codebase we wrote")
    put(f"  2. {BOLD}two HTTP APIs{RESET}         specs someone else wrote")
    put(f"  3. {BOLD}an on-chain program{RESET}   an IDL and its source", pause=1.6)
    out()
    out("Same shape comes back every time: a graph, provenance on every edge,")
    out(f"and {BOLD}a floor{RESET} — the part it will not guess.", pause=2.0)


def scene_code() -> None:
    clear()
    out(f"{BOLD}Object 1 — our own code.{RESET}", pause=0.5)
    out()
    out("A code knowledge graph, built on-device from the AST. No API key,")
    out("no model call, nothing leaves the machine.", pause=1.0)
    out()
    out(
        f"We ask about the seam this whole engine pivots on: {CYAN}auth_headers(){RESET}."
    )
    out("Every credential Gecko ever injects goes through it.", pause=1.2)
    out()

    if not GRAPH.exists():
        put(
            f"{YELLOW}graphify-out/graph.json missing — run `graphify . --code-only` first{RESET}"
        )
        return
    run(
        f"graphify explain gecko_access_authsession_auth_headers --graph {GRAPH.relative_to(REPO)}",
        pause=2.2,
    )
    out(f"{BOLD}Degree 1.{RESET}", pause=0.7)
    out()
    out("One edge — from the class that declares it. Yet it is called from the")
    out("caller, the client, and the MCP surface.", pause=1.3)
    out()
    out(
        f"{CYAN}AuthSession is a typing.Protocol.{RESET} Callers bind by SHAPE, at runtime."
    )
    out("There is no static reference for an AST to follow.", pause=1.5)
    out()
    put(
        f"{GREEN}The extraction is not wrong.{RESET} It is complete for what an AST can see."
    )
    put(
        "The seam is invisible to it because we made it a protocol on purpose.",
        pause=2.2,
    )


def scene_apis() -> None:
    clear()
    out(f"{BOLD}Object 2 — two HTTP APIs.{RESET}", pause=0.5)
    out()
    out("Two specs, written by different people, who never heard of each other.")
    out("We want one plan that crosses both.", pause=1.2)
    out()
    run(
        f"gecko export-arazzo {PEGANA} {BIRDEYE} "
        "--id pegana --id birdeye --op get-defi-multi_price --target birdeye",
        pause=2.4,
    )
    out(f"{BOLD}It refused.{RESET}", pause=0.7)
    out()
    out("Both sides have a field that looks like a token mint. Name matches,")
    out("shape matches. Gecko still will not chain them — an untrusted spec")
    out("must never mint an executable cross-API plan.", pause=1.6)
    out()
    out("So we confirm the join, as the customer, out of band:", pause=1.0)
    out()
    run(
        f"gecko export-arazzo {PEGANA} {BIRDEYE} "
        "--id pegana --id birdeye --op get-defi-multi_price --target birdeye "
        "--confirm mint=solana-token-mint --confirm list_address=solana-token-mint",
        pause=2.6,
    )
    out()
    put(f"{GREEN}Same question, now answered.{RESET} A four-hop chain across two APIs,")
    put("the join named, and portable Arazzo 1.0 anyone can run.", pause=2.2)


def scene_program() -> None:
    clear()
    out(f"{BOLD}Object 3 — an on-chain program.{RESET}", pause=0.5)
    out()
    out("Now the hardest one. A deployed program we did not write, whose IDL")
    out(
        "is incomplete, and a transaction that costs real money if it is wrong.",
        pause=1.4,
    )
    out()
    # --limit 1 so the twelve accounts the narration counts are the ONLY ones on
    # screen. Without it the second candidate prints too (honestly labelled a GUESS),
    # and a viewer could read the count against the wrong plan.
    run('gecko orquestra find-start "buy a token on pump.fun" --limit 1', pause=3.0)
    out()
    out("Twelve accounts. Every one tagged where it came from:", pause=0.9)
    put(f"  {GREEN}[extracted]{RESET}  the surface stated it")
    put(
        f"  {CYAN}[recovered]{RESET}   reconstructed from program source — the IDL dropped it"
    )
    put(f"  {YELLOW}[FLAGGED]{RESET}    Gecko does not know, and says so", pause=1.8)
    out()
    out(f"{BOLD}The FLAGGED ones are the product.{RESET}", pause=0.8)
    out("A tool that guessed there would produce a valid-looking address")
    out("for the wrong account, and you would find out after signing.", pause=2.2)


def scene_close() -> None:
    clear()
    out(f"{BOLD}Three objects. One shape.{RESET}", pause=1.0)
    out()
    put(f"  {BOLD}our code{RESET}       floor: a protocol the AST cannot follow")
    put(f"  {BOLD}two APIs{RESET}       floor: a join no spec declares")
    put(f"  {BOLD}a program{RESET}      floor: an account no IDL names", pause=2.0)
    out()
    out("Three different reasons. Same honest answer each time —")
    out(f"{BOLD}here is what I know, here is where it came from, and here is")
    out(f"what I will not guess.{RESET}", pause=2.0)
    out()
    put(f"{CYAN}Gecko does one of these for a living.{RESET}")
    put("It builds the graph. It does not run yours.", pause=2.4)
    out()
    put(
        f'{GREEN}npx @geckovision/gecko@latest prove "buy a token on pump.fun"{RESET}',
        pause=2.5,
    )


def main() -> None:
    scene_one_question()
    scene_code()
    scene_apis()
    scene_program()
    scene_close()


if __name__ == "__main__":
    main()
