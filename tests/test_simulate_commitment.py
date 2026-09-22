"""The balance before and the simulation after are read from ONE view of the chain.

Measured 2026-09-22 on mainnet: a Whirlpool deposit prepared right after the position's
open landed was refused by the spend gate for moving 6,523,520 lamports, which is the
open's rent plus both fees. The pre-balance was read at the node's default commitment,
which had not seen the open yet; the simulation, at ``processed``, had. The delta counted
the open twice.
"""

from __future__ import annotations

from typing import Any

from gecko.simulate import SIMULATION_COMMITMENT, BuiltTx, simulate

OWNER = "GpaLFMwQWh2xuBkMQGKmcYT5A1WgYJekofu6DJjp8W9c"


def test_the_pre_balance_is_read_at_the_simulation_commitment() -> None:
    seen: dict[str, Any] = {}

    def rpc(_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            seen["pre"] = params[1].get("commitment")
            return {
                "result": {"value": {"lamports": 1_000_000, "data": ["", "base64"]}}
            }
        if method == "simulateTransaction":
            seen["sim"] = params[1].get("commitment")
            return {
                "result": {
                    "value": {
                        "err": None,
                        "logs": [],
                        "unitsConsumed": 24_396,
                        "accounts": [{"lamports": 995_000, "data": ["", "base64"]}],
                    }
                }
            }
        raise AssertionError(method)

    receipt = simulate(
        {},
        rpc_url="https://rpc.example",
        rpc_call=rpc,
        build_call=lambda _p: BuiltTx(tx="QkFTRTY0VFg=", encoding="base64"),
        track=[OWNER],
        network="mainnet",
    )
    assert seen["pre"] == seen["sim"] == SIMULATION_COMMITMENT
    assert receipt.sol_delta == -5_000, (
        "only this transaction's own fee left the wallet"
    )
