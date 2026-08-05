"""Screenplay — "what an assistant can't do alone" (the post-pitch demo).

Same shape as the reference `gecko_demo_full.mp4`: one user sentence, the call an
assistant writes on its own, the real failure, then the same request through Gecko.
The closing beat names the three things that stay impossible alone even when the
call is right — try before acting, remember, notice a change.

No blame anywhere: the assistant isn't wrong, it just can't know what it can't read.

Every request is real and made during the recording (Jupiter's public quote API,
keyless). Record it:

    asciinema rec --cols 80 --rows 20 \
        -c "uv run python demo/kit/alone_vs_gecko_screenplay.py" alone.cast

    uv run --with pyte --with pillow python demo/kit/render_cast.py alone.cast \
        docs/assets/alone-vs-gecko.mp4 \
        --brand "GECKO  •  TRY THE CALL BEFORE YOU MAKE IT" \
        --scene "Gecko — one sentence, one API|what an assistant writes on its own" \
        --scene "Gecko — the same request, comprehended|mints, decimals, slippage — resolved" \
        --scene "Gecko — what's still impossible alone|try · remember · notice"
    agg --font-size 14 --theme asciinema alone.cast docs/assets/alone-vs-gecko.gif
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

API = "https://lite-api.jup.ag/swap/v1/quote"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def call(params: dict) -> tuple[int, str]:
    """Make the request for real; return (status, first line of the body)."""
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "gecko-surf/0.9.5"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---------------------------------------------------------------- scene 1
out(f"{BOLD}GECKO — WHAT AN ASSISTANT CAN'T DO ALONE{RESET}", pause=0.6)
out()
out(f"{BOLD}User:{RESET} Get me a quote for 1 SOL to USDC, half a percent slippage.", pause=1.0)
out()
out("On its own, an assistant writes the call the sentence describes:", pause=0.7)
out(f"{CYAN}$ GET /quote?inputMint=SOL&outputMint=USDC&amount=1{RESET}", 0.02)

status, body = call({"inputMint": "SOL", "outputMint": "USDC", "amount": 1})
err = json.loads(body).get("error", body)[:66] if body.startswith("{") else body[:66]
put(f"{RED}✗ HTTP {status}{RESET} — {err}", pause=0.8)
put()
put("The API wants mint addresses and lamports, not names and units.", pause=0.7)
put(f"{YELLOW}Nothing was written wrong. It just couldn't know.{RESET}", pause=1.6)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}Same sentence. Now the API is behind Gecko.{RESET}", pause=0.7)
out()
out("Gecko read the surface first, so the call carries what the API expects:", pause=0.8)
put()
put("   inputMint   So1111…1112     SOL's mint address")
put("   outputMint  EPjFWd…TDt1v    USDC's mint address")
put("   amount      1000000000      1 SOL in lamports (9 decimals)")
put("   slippageBps 50              half a percent, in basis points", pause=1.4)
put()
out(f"{CYAN}$ the same request, resolved{RESET}", 0.02)

status, body = call(
    {
        "inputMint": SOL,
        "outputMint": USDC,
        "amount": 1_000_000_000,
        "slippageBps": 50,
    }
)
q = json.loads(body)
out_usdc = int(q["outAmount"]) / 1e6
put(f"{GREEN}✓ HTTP {status}{RESET}  →  {out_usdc:,.2f} USDC", pause=0.5)
put(f"  price impact {q.get('priceImpactPct', '?')} · {len(q.get('routePlan', []))} hop · first try", pause=1.5)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}The call was the easy part.{RESET}", pause=0.7)
out()
out("Three things stay impossible alone — even when the call is right:", pause=0.8)
put()
put(f"  {RED}✗{RESET} try it first     the only way to find out is to do it")
put(f"  {RED}✗{RESET} remember         every session starts empty")
put(f"  {RED}✗{RESET} notice a change  no baseline to compare against", pause=1.6)
put()
put(f"{GREEN}Gecko is those three.{RESET}  Simulate to a receipt before spending.")
put("Outcomes kept as categories. A change gets flagged, not discovered.", pause=1.6)
put()
put(f"{BOLD}Your agent can act. Gecko is how it checks.{RESET}")
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
