"""The buy-that-passes orchestrator (gecko/providers/pumpfun_landing.py).

Offline (Pattern B, $0): an injected rpc_call serves the two control-plane reads (mint
owner, bonding_curve reserves+creator) and a canned err:null simulateTransaction; an
injected fetch_buy_instruction stands in for Orquestra /build. The bundle assembles the
STANDARD ATA prelude around the built buy and simulates to status: pass — with the
curve-quoted max_sol_cost, not a guess. The real differential (3012 vs pass on the SAME
built buy) is proven in test_landing (the prelude-off path) and, live, in the env-gated
E2E below.
"""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import pytest

from gecko.providers.pumpfun import buy_remaining_accounts, plan_buy
from gecko.providers.pumpfun_landing import (
    BuyLandingError,
    simulate_buy_landing,
)

MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"
CREATOR = "Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq"
ASSOCIATED_USER = "CTAUKpZkmejuonDJnBRW43FMZx6WpkytQrF8Cty4GfVc"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
# A valid pubkey standing in for fee_recipient in the canned offline sim (the fake RPC
# accepts anything). LIVE, the E2E resolves a REGULAR recipient — Global.fee_recipients[0]
# @ offset 162; the old "@41" guidance is refuted (@41 is a BUYBACK recipient, 6062).
FEE_RECIPIENT = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"

# Real mainnet reserves (RPC-verified) so max_sol_cost is a known value: 1_000_000 units →
# base 29 lamports, +5% default slippage → 31.
REAL_VTOKEN = 1_065_644_468_288_345
REAL_VSOL = 30_207_084_547
REAL_RTOKEN = 785_744_468_288_345

# bonding_curve_v2 (seed ["bonding-curve-v2", mint]) — a Class-1 hidden remaining account.
BONDING_CURVE_V2 = "5VHjhM7qJfaKd9skGBkU1nZCZ9r8XnzEeJ6wNGtwddyq"
# 8 canned buyback fee recipients for the fake Global blob (any valid pubkeys).
BUYBACK = [
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
]

CANNED_BUY = {
    "name": "buy",
    "programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "data": "66063d1201daebea40420f000000000080f0fa020000000001",
    "accounts": [
        {"pubkey": GLOBAL, "isSigner": False, "isWritable": False},
        {"pubkey": USER, "isSigner": True, "isWritable": True},
    ],
}


def _creator_bytes() -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(CREATOR))


def _pubkey_bytes(addr: str) -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(addr))


def _bonding_curve_blob() -> str:
    """A 151-byte BondingCurve blob carrying BOTH the reserves (offsets 8/16/24) and the
    creator (offset 49) — the single read plan_buy + the curve both consume."""
    raw = bytearray(151)
    struct.pack_into("<Q", raw, 8, REAL_VTOKEN)
    struct.pack_into("<Q", raw, 16, REAL_VSOL)
    struct.pack_into("<Q", raw, 24, REAL_RTOKEN)
    raw[49:81] = _creator_bytes()
    return base64.b64encode(bytes(raw)).decode()


def _global_blob() -> str:
    """A Global blob carrying the 8 buyback_fee_recipients at offset 741 (the array a
    post-upgrade buy needs as remaining accounts)."""
    raw = bytearray(1045)
    for i, addr in enumerate(BUYBACK):
        raw[741 + i * 32 : 741 + i * 32 + 32] = _pubkey_bytes(addr)
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc(sim_value: dict[str, Any]):
    bc_blob = _bonding_curve_blob()
    g_blob = _global_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            addr = params[0]
            if addr == MINT:
                return {"result": {"value": {"owner": TOKEN_2022}}}
            if addr == BONDING_CURVE:
                return {
                    "result": {"value": {"owner": "x", "data": [bc_blob, "base64"]}}
                }
            if addr == GLOBAL:
                return {"result": {"value": {"owner": "x", "data": [g_blob, "base64"]}}}
            return {"result": {"value": {"lamports": 5_000_000}}}  # user (track)
        if method == "simulateTransaction":
            return {"result": {"value": sim_value}}
        raise AssertionError(f"unexpected method {method}")

    return rpc


def _bindings() -> dict[str, Any]:
    return {
        "mint": MINT,
        "user": USER,
        "amount": 1_000_000,
        "fee_recipient": FEE_RECIPIENT,
        "track_volume": True,
    }


def test_missing_fee_recipient_raises() -> None:
    with pytest.raises(BuyLandingError):
        simulate_buy_landing(
            {"mint": MINT, "user": USER, "amount": 1, "track_volume": True},
            rpc_call=_fake_rpc({"err": None, "unitsConsumed": 1, "logs": []}),
        )


def test_landing_bundle_simulates_to_pass_with_curve_quoted_arg() -> None:
    captured: dict[str, Any] = {}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        captured["accounts"] = accounts
        captured["args"] = args
        return CANNED_BUY

    value = {"err": None, "unitsConsumed": 50_000, "logs": ["Program success"]}
    result = simulate_buy_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(value),
        fetch_buy_instruction=fetch,
        include_derive_only=False,
    )
    # the a-ha: the assembled landing bundle lands
    assert result.landing_receipt.status == "pass"
    assert result.landing_receipt.revert_class is None
    # max_sol_cost is CURVE-QUOTED, not guessed (base 29 → +5% → 31), and reaches /build
    assert result.base_sol_cost == 29
    assert result.max_sol_cost == 31
    assert captured["args"]["max_sol_cost"] == 31
    # fee_recipient (the honest gap) is filled for the build
    assert captured["accounts"]["fee_recipient"] == FEE_RECIPIENT
    # CU limit set from measured units (50_000 × 1.2)
    assert result.unit_limit == 60_000


def test_result_declares_the_ordered_landing_plan() -> None:
    value = {"err": None, "unitsConsumed": 50_000, "logs": []}
    result = simulate_buy_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(value),
        fetch_buy_instruction=lambda a, ar, fp: CANNED_BUY,
        include_derive_only=False,
    )
    kinds = [step["kind"] for step in result.landing_plan]
    assert kinds == ["compute_budget", "compute_budget", "create_idempotent_ata", "buy"]
    ata = next(s for s in result.landing_plan if s["kind"] == "create_idempotent_ata")
    assert ata["accounts"]["ata"] == ASSOCIATED_USER
    assert ata["accounts"]["token_program"] == TOKEN_2022


def test_result_declares_recovered_hidden_remaining_accounts() -> None:
    # The Class-1 recovery: the buy step declares bonding_curve_v2 then the 8 buyback
    # recipients as remaining_accounts — the accounts the IDL drops, so Orquestra/1claw
    # include them in the real, signable tx.
    value = {"err": None, "unitsConsumed": 80_000, "logs": []}
    result = simulate_buy_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(value),
        fetch_buy_instruction=lambda a, ar, fp: CANNED_BUY,
        include_derive_only=False,
    )
    buy = next(s for s in result.landing_plan if s["kind"] == "buy")
    remaining = [r["pubkey"] for r in buy["remaining_accounts"]]
    assert remaining == [BONDING_CURVE_V2, *BUYBACK]
    assert all(r["isWritable"] and not r["isSigner"] for r in buy["remaining_accounts"])
    # the resolver-style recipe plan_buy declares is RESOLVED here, so it is dropped
    assert "remaining_accounts_unresolved" not in buy


def test_declared_plan_and_orchestrator_assembly_do_not_drift() -> None:
    """The no-drift guard: plan_buy's DECLARED buy step and the set simulate_buy_landing
    actually assembles are the same truth — same named accounts, the declared concrete
    prefix (bonding_curve_v2) is the orchestrator's prefix, and the orchestrator's full
    set is exactly buy_remaining_accounts(bonding_curve_v2, <the declared @741 recipe
    resolved>). If either side changes alone, this fails."""
    value = {"err": None, "unitsConsumed": 80_000, "logs": []}
    result = simulate_buy_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(value),
        fetch_buy_instruction=lambda a, ar, fp: CANNED_BUY,
        include_derive_only=False,
    )
    plan = plan_buy(
        {
            "mint": MINT,
            "user": USER,
            "amount": 1_000_000,
            "max_sol_cost": result.max_sol_cost,
            "track_volume": True,
        },
        rpc_call=_fake_rpc(value),
    )
    declared = next(s for s in plan["landing_plan"] if s["kind"] == "buy")
    verified = next(s for s in result.landing_plan if s["kind"] == "buy")

    # named accounts: identical (incl. bonding_curve_v2, the declared account #17)
    assert verified["accounts"] == declared["accounts"]
    # the declared concrete prefix IS the orchestrator's prefix …
    prefix = declared["remaining_accounts"]
    assert verified["remaining_accounts"][: len(prefix)] == prefix
    # … and resolving the declared @741 recipe completes the exact orchestrator set,
    # via the one shared assembly function (single source of truth).
    recipe = declared["remaining_accounts_unresolved"]["buyback_fee_recipient"][
        "resolve"
    ]
    assert (recipe["read"], recipe["field_offset"], recipe["count"]) == (
        "global",
        741,
        8,
    )
    assert verified["remaining_accounts"] == buy_remaining_accounts(
        BONDING_CURVE_V2, BUYBACK
    )


# --- env-gated real E2E: the side-by-side a-ha on a surfpool mainnet fork -----


@pytest.mark.skipif(
    os.getenv("GECKO_SIMULATE_E2E") != "1",
    reason="set GECKO_SIMULATE_E2E=1 (+ surfpool + a mainnet RPC) to run the live a-ha",
)
def test_buy_that_passes_e2e_side_by_side() -> None:
    """Real Orquestra /build → assemble the landing bundle → simulate on a surfpool fork.
    The derive-only path (no ATA prelude) reverts 3012; the Gecko-complete landing bundle
    passes. Prints the verbatim side-by-side — this is the deliverable."""
    from gecko.pda_testkit import SurfpoolError, SurfpoolFork

    from gecko.pda_resolve import read_account_field_pubkey

    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    user = os.getenv("GECKO_E2E_USER", USER)
    try:
        with SurfpoolFork(mainnet) as fork:
            # Resolve a valid regular fee_recipient empirically: Global.fee_recipients[0] @
            # data offset 162 (NOT Global.fee_recipient @41 — that is a *buyback* recipient,
            # which the live Receipt refuted with 6062). The gap-map's "let the sim confirm".
            fee_recipient = os.getenv(
                "GECKO_E2E_FEE_RECIPIENT"
            ) or read_account_field_pubkey(GLOBAL, 162, rpc_url=fork.rpc_url)
            result = simulate_buy_landing(
                {
                    "mint": MINT,
                    "user": user,
                    "amount": 1_000_000,
                    "fee_recipient": fee_recipient,
                    "track_volume": True,
                },
                rpc_url=fork.rpc_url,
                include_derive_only=True,
                network_label="surfpool fork (mainnet-backed — NOT mainnet)",
            )
    except SurfpoolError as exc:
        pytest.skip(f"surfpool fork unavailable: {exc}")

    derive = result.derive_only_receipt
    land = result.landing_receipt
    assert derive is not None

    # VERDICT FIRST — the naive line must not be readable as OUR failure: the revert is
    # the expected, demonstrated gap; the Gecko bundle passing is the deliverable. The
    # labels only claim what each receipt actually says (honesty over polish).
    naive_code = _anchor_custom_code(derive.err)
    naive_line = (
        "❌ NAIVE (derive-only) — EXPECTED revert, this is the gap: "
        f"{derive.revert_class}{f' ({naive_code})' if naive_code is not None else ''}"
        if derive.status == "fail"
        else f"NAIVE (derive-only) — status={derive.status} (a revert was expected; see logs)"
    )
    gecko_line = (
        f"✅ GECKO landing bundle — PASSES: {land.units_consumed:,} CU"
        if land.status == "pass" and land.units_consumed is not None
        else f"GECKO landing bundle — status={land.status} revert_class={land.revert_class}"
    )
    print("\n=== pump buy: naive derive-only vs GECKO landing bundle ===")
    print(naive_line)
    print(gecko_line)
    print(
        "RESULT: the naive path reverts on mainnet; the Gecko bundle lands — "
        "caught for $0 before any spend."
    )

    # full detail AFTER the verdict, for anyone auditing the run
    print("--- details ---")
    print(
        f"base_sol_cost={result.base_sol_cost} max_sol_cost={result.max_sol_cost} "
        f"cu_limit={result.unit_limit}"
    )
    print(
        f"naive derive-only: status={derive.status} revert_class={derive.revert_class}"
    )
    print(f"  logs_tail={list(derive.logs_tail)}")
    print(
        f"gecko landing bundle: status={land.status} units={land.units_consumed} "
        f"revert_class={land.revert_class}"
    )
    print(f"  logs_tail={list(land.logs_tail)}")
    print(f"network: {land.network_label}")

    # the differential is the thesis: derive-only reverts on the buyer's ATA …
    assert derive.status == "fail"
    assert derive.revert_class == "account_error"
    # … and the assembled landing bundle lands.
    assert land.status == "pass"


def _anchor_custom_code(err: object) -> int | None:
    """The Anchor ``Custom`` code from a sim ``err`` (e.g. 3012), if present — printed so
    the naive verdict line names the exact expected revert, never a fabricated number."""
    if isinstance(err, dict):
        instruction_error = err.get("InstructionError")
        if isinstance(instruction_error, list) and len(instruction_error) == 2:
            detail = instruction_error[1]
            if isinstance(detail, dict) and isinstance(detail.get("Custom"), int):
                return int(detail["Custom"])
    return None
