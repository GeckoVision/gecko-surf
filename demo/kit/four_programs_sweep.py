"""Run all four program flows for real against a live surfpool fork, concurrently.

Used by the four-program screenplay so every line on screen came off the wire during
the recording. Each flow returns (label, naive_verdict, gecko_verdict); a flow that
cannot be run right now (no claimable miner, no open funding window, no holder) says
so honestly instead of being faked or omitted.

    uv run python demo/kit/four_programs_sweep.py        # prints the sweep
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
# the E2E modules import their siblings unqualified (pytest puts rootdir/tests on the
# path); mirror that so the discovery helpers are reusable outside pytest.
sys.path.insert(0, os.path.join(_ROOT, "tests"))

RPC = os.environ.get("GECKO_DEMO_RPC", "http://127.0.0.1:8899")
MAINNET = os.environ.get("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")

PUMP_MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
PUMP_USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"


def rpc(rpc_url: str, method: str, params: list) -> dict:
    from gecko.rpc import default_rpc_call

    if method == "getAccountInfo":
        params = list(params)
        opts = dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        opts["encoding"] = "base64"
        params = [params[0], opts]
    return default_rpc_call(rpc_url, method, params)


def _verdicts(result) -> tuple[str, str]:
    """(naive, gecko) as short display strings, straight from the two receipts."""
    naive = result.derive_only_receipt
    if naive is None:
        bad = "not run"
    elif naive.status == "pass":
        bad = "also lands"
    else:
        bad = naive.revert_class or "fail"
    good = (
        f"PASS {result.landing_receipt.units_consumed:,} CU"
        if result.landing_receipt.status == "pass"
        else f"FAIL {result.landing_receipt.revert_class}"
    )
    return bad, good


def pump_buy() -> tuple[str, str, str]:
    from gecko.pda import derive_pda
    from gecko.pda_resolve import read_account_field_pubkey
    from gecko.provider_config import load_packaged_provider
    from gecko.providers.pumpfun_landing import simulate_buy_landing

    _, apis = load_packaged_provider("orquestra")
    fee = read_account_field_pubkey(
        derive_pda(apis["pumpfun"].program.pdas["global"], {}).address,
        162,
        rpc_url=RPC,
        rpc_call=rpc,
    )
    r = simulate_buy_landing(
        {
            "mint": PUMP_MINT,
            "user": PUMP_USER,
            "amount": 1_000_000,
            "fee_recipient": fee,
            "track_volume": True,
        },
        rpc_url=RPC,
        rpc_call=rpc,
    )
    return ("pump.fun  buy", *_verdicts(r))


def ore_claim() -> tuple[str, str, str]:
    from test_ore_claim_landing import _discover_claimable_miner
    from gecko.providers.ore_landing import simulate_claim_landing

    found = _discover_claimable_miner(MAINNET)
    if not found:
        return ("ore       claim", "no claimable miner right now", "—")
    r = simulate_claim_landing(dict(found), rpc_url=RPC, rpc_call=rpc)
    return ("ore       claim", *_verdicts(r))


def metadao_fund() -> tuple[str, str, str]:
    from test_metadao_fund_landing import _discover_live_launch_and_funder
    from gecko.providers.metadao_landing import simulate_fund_landing

    found = _discover_live_launch_and_funder(MAINNET, 10_000)
    if not found:
        return ("metadao   fund", "no open funding window", "—")
    r = simulate_fund_landing(dict(found), rpc_url=RPC, rpc_call=rpc)
    return ("metadao   fund", *_verdicts(r))


FLOWS = {"pump": pump_buy, "ore": ore_claim, "metadao": metadao_fund}


def sweep(on_result=None) -> list[tuple[str, str, str]]:
    """Run every flow concurrently; call on_result(row) as each lands."""
    rows: list[tuple[str, str, str]] = []
    with ThreadPoolExecutor(max_workers=len(FLOWS)) as pool:
        futs = {pool.submit(fn): name for name, fn in FLOWS.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # never fabricate a result
                row = (f"{name:<9} —", f"unavailable: {type(exc).__name__}", "—")
            rows.append(row)
            if on_result:
                on_result(row)
    return rows


if __name__ == "__main__":
    import time

    t0 = time.time()
    for label, bad, good in sweep(lambda r: print(f"  {r[0]:<16} naive: {r[1]:<34} gecko: {r[2]}")):
        pass
    print(f"\nswept in {time.time() - t0:.1f}s")
