"""Task 5: the real-RPC / real-Orquestra smoke — env-gated, the LAST rung.

With ``GECKO_SIMULATE_E2E=1`` this builds a REAL Pump.fun ``buy`` via the live Orquestra
``/build`` slug, supplies a ``fee_recipient`` candidate (``Global.fee_recipient`` @ data
offset 41, read from the fork), and ``simulateTransaction``s it against a surfpool mainnet
fork (preferred, $0) or ``GECKO_MAINNET_RPC``. It asserts a Receipt comes back — a PASS or a
CLASSIFIED fail — and PRINTS it. A classified fail is itself the honest finding.

This is the standing "revert caught before mainnet" proof. As observed (2026-08-04) it
catches a REAL revert: ``AnchorError 3012 (associated_user AccountNotInitialized)`` →
``revert_class="account_error"`` — the buyer holds no ATA, so a real buy would revert and
burn fees on-chain. Gecko flags that precondition (see ``plan_buy`` ``preconditions``) and
the Receipt catches it here for $0. It exercises the full corrected live path: ``plan_buy``
emits the ``OptionBool`` arg shape, the engine prefers ``serializedTransaction`` and passes
the builder's base58 encoding through, and the UA fix clears Cloudflare's bot check.

Never signs, never broadcasts. The default suite stays offline; this only runs when the
env flag is set AND surfpool + a mainnet RPC + network are available.
"""

from __future__ import annotations

import os

import pytest

from gecko.pda_resolve import read_account_field_pubkey
from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
)
from gecko.providers.pumpfun import plan_buy
from gecko.simulate import Receipt, SimulateError, simulate

pytestmark = pytest.mark.skipif(
    os.getenv("GECKO_SIMULATE_E2E") != "1",
    reason="set GECKO_SIMULATE_E2E=1 (+ surfpool + a mainnet RPC + network) to run the sim smoke",
)

MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
# Global.fee_recipient lives at data offset 41 (8-byte discriminator + 1 bool + 32-byte
# authority). This is the CANDIDATE — the whole point is to see if buy validates against it.
GLOBAL_FEE_RECIPIENT_OFFSET = 41


def test_simulate_real_pump_buy_returns_a_receipt() -> None:
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    bindings = {
        "mint": MINT,
        "user": USER,
        "amount": 1_000_000,
        "max_sol_cost": 50_000_000,
        "track_volume": True,
    }
    try:
        with SurfpoolFork(mainnet) as fork:
            rpc_url = fork.rpc_url
            plan = plan_buy(bindings, rpc_url=rpc_url)

            # read the fee_recipient CANDIDATE from Global @ offset 41 (control-plane read)
            fee_recipient = read_account_field_pubkey(
                plan["accounts"]["global"],
                GLOBAL_FEE_RECIPIENT_OFFSET,
                rpc_url=rpc_url,
            )
            merged = {
                **plan,
                "accounts": {**plan["accounts"], "fee_recipient": fee_recipient},
            }

            receipt = simulate(merged, rpc_url=rpc_url, track=[USER])
    except SurfpoolError as exc:
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"surfpool unavailable: {exc}")
    except SimulateError as exc:
        # A build-transport failure (e.g. the live Orquestra /build needs auth) is an
        # ENVIRONMENT/access gap, not a refutation of the Receipt engine — surface it
        # honestly rather than masquerade as a pass or a program revert.
        pytest.skip(f"could not build the tx (transport/access, not a revert): {exc}")

    print("\n=== Pump.fun buy — simulate Receipt (surfpool fork, NOT mainnet) ===")
    print("fee_recipient candidate:", fee_recipient)
    print("status:      ", receipt.status)
    print("revert_class:", receipt.revert_class)
    print("units:       ", receipt.units_consumed)
    print("sol_delta:   ", receipt.sol_delta)
    print("network:     ", receipt.network_label)
    for line in receipt.logs_tail:
        print("  ", line)

    assert isinstance(receipt, Receipt)
    # a PASS or a CLASSIFIED fail are both valid honest outcomes
    assert receipt.status in {"pass", "fail"}
    if receipt.status == "fail":
        assert receipt.revert_class is not None
