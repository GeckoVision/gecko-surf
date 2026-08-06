"""Screenplay — "the alert that couldn't do anything" (the cross-surface demo).

A peg oracle says an asset is drifting. Acting on that means calling a Solana program —
and the program surface declares 9 accounts while the instruction that lands needs 25.
The other 16 are the legs of whichever route an HTTP aggregator picked a second ago.

Every number on screen is read live during the take: the peg states come from the oracle,
the account counts and the receipt come from a real run against a surfpool mainnet fork.
Nothing is typed in by hand and nothing is pinned.

Record it (surfpool must ALREADY be running — the fork is the environment, not the story):

    surfpool start --no-tui --no-deploy --rpc-url "$GECKO_MAINNET_RPC" --port 8899 &
    asciinema rec --cols 80 --rows 20 \
        -c "uv run python demo/kit/peg_to_exit_screenplay.py" peg.cast

    # leak-check the take (no key material is read, but check anyway)
    grep -nE "[A-Za-z0-9_-]{40,}" peg.cast | head

    uv run --with pyte --with pillow python demo/kit/render_cast.py peg.cast \
        docs/assets/peg-to-exit.mp4 \
        --brand "GECKO  •  TRY THE CALL BEFORE YOU MAKE IT" \
        --scene "Gecko — the alert that can't act|an oracle reports. it cannot execute." \
        --scene "Gecko — what the surface can't carry|9 declared · 25 needed" \
        --scene "Gecko — the receipt|simulate before you spend · \$0 · never signs"

Honesty contract (demo/kit/README.md): one unedited take; the fork is labelled on screen
wherever a number appears; nothing claimed that the run did not show.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from gecko.providers.jupiter_landing import plan_route, simulate_route_landing  # noqa: E402
from gecko.rpc import default_rpc_call  # noqa: E402
from screenplay import BOLD, CYAN, GREEN, RED, RESET, YELLOW, clear, out, put  # noqa: E402

RPC = os.environ.get("GECKO_DEMO_RPC", "http://127.0.0.1:8899")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# A system-owned wallet that actually holds the asset. The largest holders are program
# PDAs, which cannot pay a fee — the sim says InvalidAccountForFee, honestly.
HOLDER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"


def _rpc(rpc_url: str, method: str, params: list) -> dict:
    if method == "getAccountInfo":
        params = list(params)
        opts = (
            dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        )
        opts["encoding"] = "base64"
        params = [params[0], opts]
    return default_rpc_call(rpc_url, method, params)


# ---------------------------------------------------------------- scene 1
out(f"{BOLD}A risk oracle watching 68 assets on Solana.{RESET}", pause=0.5)
out("It knows, minutes early, when one starts leaving its peg.", pause=0.8)
out()
out(f"{CYAN}$ # asking it what is drifting right now{RESET}", 0.02)

_feed = json.loads(
    urllib.request.urlopen("https://api.pegana.xyz/v1/assets", timeout=25).read()
)["data"]
_off = [a for a in _feed if a["state"] != "PEGGED"]
put(f"  {len(_feed)} assets tracked · {len(_feed) - len(_off)} pegged", pause=0.4)
for asset in _off:
    put(
        f"  {YELLOW}{asset['state']}{RESET}  {asset['symbol']}  ({asset['class']})",
        pause=0.4,
    )
put()
put(
    f"{YELLOW}The alert is right, and early. Now something has to act on it.{RESET}",
    pause=1.6,
)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}Acting means one swap. Here is what the surface offers.{RESET}", pause=0.6)
out()
out(f"{CYAN}$ # the published program surface for that swap{RESET}", 0.02)

_target = (
    _off[0]
    if _off
    else {"symbol": "hyUSD", "mint": "5YMkXAYccHSGnHn9nob9xEvv6Pvka9DZWH7nTbotTu9E"}
)
BINDINGS = {
    "input_mint": _target["mint"],
    "output_mint": USDC,
    "amount": 10_000_000,
    "user": HOLDER,
}
_plan = plan_route(BINDINGS)

put(
    f"  declares {_plan['declared_account_count']} accounts, 0 derivable PDAs",
    pause=0.8,
)
put()
out(f"{CYAN}$ # what the call actually needs to exist{RESET}", 0.02)
put(
    f"  {RED}{len(_plan['accounts'])} accounts{RESET} + {len(_plan['lookup_tables'])} lookup table",
    pause=0.9,
)
put()
put(
    "  the difference is the route — chosen a second ago, by a different API.",
    pause=1.0,
)
put(
    f"{YELLOW}No schema can hold those. They aren't facts about the program.{RESET}",
    pause=1.7,
)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}Same intent. Both surfaces. Simulated first.{RESET}", pause=0.5)
out()

_result = simulate_route_landing(BINDINGS, rpc_url=RPC, rpc_call=_rpc)
_counts = _result.provenance_counts

put("  every account says where it came from:", pause=0.4)
put(
    f"   {_counts.get('extracted', 0):>3}  extracted      declared by the program",
    pause=0.3,
)
put(
    f"   {_counts.get('recovered', 0):>3}  recovered      seed read from program source",
    pause=0.3,
)
put(
    f"   {_counts.get('cross_surface', 0):>3}  cross-surface  only the other API knew",
    pause=0.9,
)
put()
put(f"  route: {' → '.join(_result.route_labels)}", pause=0.5)
put()
out(f"{CYAN}$ # the receipt{RESET}", 0.02)

_naive = _result.derive_only_receipt
put(
    f"{RED}✗{RESET} the program surface alone   "
    f"{'could not be assembled' if _naive is None else _naive.status}",
    pause=0.5,
)
put(
    f"{GREEN}✓{RESET} both surfaces, comprehended  PASS  "
    f"{_result.landing_receipt.units_consumed:,} compute units",
    pause=0.6,
)
put(f"  {_result.landing_receipt.network_label}", pause=1.4)
put()
put(f"{YELLOW}An oracle reports. It cannot execute.{RESET}", pause=0.9)
put(f"{BOLD}Gecko is how the alert becomes an action that lands.{RESET}", pause=0.8)
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
