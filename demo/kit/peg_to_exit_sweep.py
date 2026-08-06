"""Peg signal → exit route → Receipt. One run, three surfaces, $0, nothing signed.

The chain a single agent has to cross to act on a risk alert:

  a peg oracle names the asset that is drifting  (HTTP surface)
      → an aggregator names the venue that can absorb the exit  (HTTP surface)
          → the program surface names the instruction  (Solana program)
              → simulate on a fork  → a Receipt

The finding is the middle: the program surface declares 9 accounts for `route`, and the
instruction that lands carries 25. The other 16 are the legs of whichever route the
aggregator picked at that instant — they are not in any IDL, and cannot be, because they
are not facts about the program.

    surfpool start --no-tui --no-deploy --rpc-url "$GECKO_MAINNET_RPC" --port 8899 &
    uv run python demo/kit/peg_to_exit_sweep.py
"""

import json
import os
import sys
import urllib.request

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
from gecko.rpc import default_rpc_call
from gecko.providers.jupiter_landing import simulate_route_landing, plan_route

RPC = os.environ.get("GECKO_DEMO_RPC", "http://127.0.0.1:8899")


def rpc(url, method, params):
    if method == "getAccountInfo":
        params = list(params)
        o = dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        o["encoding"] = "base64"
        params = [params[0], o]
    return default_rpc_call(url, method, params)


# 1. the peg oracle names the asset that is drifting
peg = json.loads(
    urllib.request.urlopen("https://api.pegana.xyz/v1/assets", timeout=25).read()
)
drifting = [a for a in peg["data"] if a["state"] in ("DRIFT", "DEPEG", "CRITICAL")]
print(f"PEGANA  {len(peg['data'])} assets tracked, {len(drifting)} not PEGGED")
for a in drifting:
    print(
        f"   {a['symbol']:8} {a['class']:11} {a['state']:6} disc={a['discount']} anchor={a['anchor']}"
    )
if not drifting:
    print("   (nothing drifting right now — using hyUSD as the worked example)")
target = (
    drifting[0]
    if drifting
    else {"symbol": "hyUSD", "mint": "5YMkXAYccHSGnHn9nob9xEvv6Pvka9DZWH7nTbotTu9E"}
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
b = {
    "input_mint": target["mint"],
    "output_mint": USDC,
    "amount": 10_000_000,
    "user": USER,
}

# 2. what the PROGRAM SURFACE alone can offer
plan = plan_route(b)
print(
    f"\nORQUESTRA program surface declares : {plan['declared_account_count']} accounts, 0 PDAs"
)
print(
    f"the instruction that lands carries : {len(plan['accounts'])} accounts "
    f"+ {len(plan['lookup_tables'])} lookup table(s)"
)
print(
    f"route: {' -> '.join(plan['route_labels'])}   price impact {plan['price_impact_pct']}%"
)

# 3. simulate both
r = simulate_route_landing(b, rpc_url=RPC, rpc_call=rpc)
print("\nprovenance of the 25 accounts:", r.provenance_counts)
n = r.derive_only_receipt
print(
    f"\nprogram surface alone : {'not even assemblable' if n is None else (n.status + ' / ' + str(n.revert_class))}"
)
print(
    f"Gecko-complete        : {r.landing_receipt.status.upper()}  "
    f"{r.landing_receipt.units_consumed:,} CU"
    if r.landing_receipt.units_consumed
    else r.landing_receipt.status
)
print(f"network               : {r.landing_receipt.network_label}")
print(
    f"quoted                : {int(r.in_amount) / 1e6:.4f} {target['symbol']} -> {int(r.out_amount) / 1e6:.4f} USDC"
)
