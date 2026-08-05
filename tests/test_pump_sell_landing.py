"""The sell-that-passes orchestrator (gecko/providers/pumpfun_landing.simulate_sell_landing).

Offline (Pattern B, $0): an injected ``rpc_call`` serves the control-plane reads (mint
owner, bonding_curve reserves + creator + shape flags, Global's buyback recipients) and a
canned ``simulateTransaction``; an injected ``fetch_sell_instruction`` stands in for
Orquestra ``/build``. Nothing here touches the network.

The sell's gap is sharper than the buy's because there is no prelude to blame: the seller
already holds the token, so ``associated_user`` exists and the entire prelude is
ComputeBudget. What is missing from the builder's 14-account instruction is the appended
set the surface mentions ONLY in an English doc-comment — and, since April 2026, a buyback
fee recipient. The live E2E at the bottom prints the labelled side-by-side.
"""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import pytest

from gecko.providers.pumpfun import plan_sell, sell_remaining_accounts
from gecko.providers.pumpfun_landing import SellLandingError, simulate_sell_landing

MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"
CREATOR = "Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
FEE_RECIPIENT = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
BONDING_CURVE_V2 = "5VHjhM7qJfaKd9skGBkU1nZCZ9r8XnzEeJ6wNGtwddyq"
USER_VOLUME_ACCUMULATOR = "Dhmt8HLWC5KFC3t3RzLiFrqgJwxuR6EgiVssbF34g8CL"

# Real mainnet reserves (RPC-verified) so min_sol_output is a KNOWN value, hand-checked
# against the SDK formula: 1e12 base units → 28_319_731 lamports pre-fee, −5% → 26_903_744.
REAL_VTOKEN = 1_065_644_468_288_345
REAL_VSOL = 30_207_084_547
REAL_RTOKEN = 785_744_468_288_345
AMOUNT = 1_000_000_000_000
BASE_OUTPUT = 28_319_731
MIN_OUTPUT = 26_903_744

# The 8 buyback recipients of the April-2026 upgrade (BREAKING_FEE_RECIPIENT.md); a sell
# appends exactly ONE of them, deterministically index 0.
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

# What Orquestra's /build actually returns for `sell` (wire-verified 2026-08): a
# 14-account instruction, discriminator 33e685a4017f83ad + amount + min_sol_output.
CANNED_SELL = {
    "name": "sell",
    "programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "data": "33e685a4017f83ad0010a5d4e80000000100000000000000",
    "accounts": [
        {"pubkey": GLOBAL, "isSigner": False, "isWritable": False},
        {"pubkey": USER, "isSigner": True, "isWritable": True},
    ],
}


def _pubkey_bytes(addr: str) -> bytes:
    from solders.pubkey import Pubkey

    return bytes(Pubkey.from_string(addr))


def _bonding_curve_blob(cashback: bool = False) -> str:
    """A 151-byte BondingCurve blob carrying the reserves (8/16/24), the creator (49) and
    the shape flag (82) — the single account both the quote and plan_sell consume."""
    raw = bytearray(151)
    struct.pack_into("<Q", raw, 8, REAL_VTOKEN)
    struct.pack_into("<Q", raw, 16, REAL_VSOL)
    struct.pack_into("<Q", raw, 24, REAL_RTOKEN)
    raw[49:81] = _pubkey_bytes(CREATOR)
    if cashback:
        raw[82] = 1
    return base64.b64encode(bytes(raw)).decode()


def _global_blob() -> str:
    raw = bytearray(1045)
    for i, addr in enumerate(BUYBACK):
        raw[741 + i * 32 : 741 + i * 32 + 32] = _pubkey_bytes(addr)
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc(sim_value: dict[str, Any], *, cashback: bool = False):
    bc_blob = _bonding_curve_blob(cashback)
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
        "amount": AMOUNT,
        "fee_recipient": FEE_RECIPIENT,
    }


def _run(*, cashback: bool = False, capture: dict[str, Any] | None = None):
    value = {"err": None, "unitsConsumed": 45_000, "logs": ["Program success"]}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        if capture is not None:
            capture["accounts"] = dict(accounts)
            capture["args"] = dict(args)
            capture["fee_payer"] = fee_payer
        return CANNED_SELL

    return simulate_sell_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(value, cashback=cashback),
        fetch_sell_instruction=fetch,
        include_derive_only=False,
    )


# --- bindings + the curve-quoted arg ----------------------------------------------


def test_missing_fee_recipient_raises() -> None:
    with pytest.raises(SellLandingError):
        simulate_sell_landing(
            {"mint": MINT, "user": USER, "amount": 1},
            rpc_call=_fake_rpc({"err": None, "unitsConsumed": 1, "logs": []}),
        )


def test_landing_bundle_passes_with_a_curve_quoted_floor() -> None:
    capture: dict[str, Any] = {}
    result = _run(capture=capture)
    assert result.landing_receipt.status == "pass"
    assert result.landing_receipt.revert_class is None
    # min_sol_output is CURVE-QUOTED off the SELL formula and reaches /build
    assert (result.base_sol_output, result.min_sol_output) == (BASE_OUTPUT, MIN_OUTPUT)
    assert capture["args"]["min_sol_output"] == MIN_OUTPUT
    assert capture["args"]["amount"] == AMOUNT
    # the honest gap is filled for the build, and only for the build
    assert capture["accounts"]["fee_recipient"] == FEE_RECIPIENT
    # CU limit from measured units (45_000 × 1.2)
    assert result.unit_limit == 54_000


def test_appended_accounts_are_stripped_from_the_build_payload() -> None:
    """/build is driven by the pre-upgrade IDL, whose `sell` names 14 accounts and knows
    neither bonding_curve_v2 nor user_volume_accumulator — sending them would be rejected.
    They travel as remaining accounts instead."""
    capture: dict[str, Any] = {}
    _run(cashback=True, capture=capture)
    assert "bonding_curve_v2" not in capture["accounts"]
    assert "user_volume_accumulator" not in capture["accounts"]
    assert len(capture["accounts"]) == 14  # the 13 derived + fee_recipient
    assert capture["fee_payer"] == USER


# --- the recovered appended set (the whole point) ---------------------------------


def test_non_cashback_run_recovers_two_appended_accounts() -> None:
    result = _run(cashback=False)
    assert result.is_cashback_coin is False
    assert result.account_count == 16
    sell = next(s for s in result.landing_plan if s["kind"] == "sell")
    assert sell["remaining_accounts"] == [
        {"pubkey": BONDING_CURVE_V2, "isWritable": False, "isSigner": False},
        {"pubkey": BUYBACK[0], "isWritable": True, "isSigner": False},
    ]
    # the resolver-style recipe plan_sell declares is RESOLVED here, so it is dropped
    assert "remaining_accounts_unresolved" not in sell


def test_cashback_run_prepends_the_user_volume_accumulator() -> None:
    result = _run(cashback=True)
    assert result.is_cashback_coin is True
    assert result.account_count == 17
    sell = next(s for s in result.landing_plan if s["kind"] == "sell")
    assert [entry["pubkey"] for entry in sell["remaining_accounts"]] == [
        USER_VOLUME_ACCUMULATOR,
        BONDING_CURVE_V2,
        BUYBACK[0],
    ]
    # metas per pump-sdk: accumulator writable, bonding_curve_v2 READ-ONLY, buyback writable
    assert [entry["isWritable"] for entry in sell["remaining_accounts"]] == [
        True,
        False,
        True,
    ]


def test_exactly_one_buyback_recipient_is_appended() -> None:
    """A buy appends all 8; a sell must append exactly ONE or the published totals
    (16/17) cannot hold. Deterministically index 0."""
    result = _run()
    sell = next(s for s in result.landing_plan if s["kind"] == "sell")
    appended = [entry["pubkey"] for entry in sell["remaining_accounts"]]
    assert sum(1 for pubkey in appended if pubkey in BUYBACK) == 1
    assert appended[-1] == BUYBACK[0]


def test_landing_plan_has_no_ata_prelude() -> None:
    kinds = [step["kind"] for step in _run().landing_plan]
    assert kinds == ["compute_budget", "compute_budget", "sell"]


# --- the no-drift guard ------------------------------------------------------------


@pytest.mark.parametrize("cashback", [False, True])
def test_declared_plan_and_orchestrator_assembly_do_not_drift(cashback: bool) -> None:
    """plan_sell's DECLARED sell step and the set simulate_sell_landing actually
    assembles are the same truth — same named accounts, the declared concrete prefix is
    the orchestrator's prefix, and resolving the declared @741 recipe completes the exact
    orchestrator set through the ONE shared assembly function. If either side changes
    alone, this fails."""
    result = _run(cashback=cashback)
    value = {"err": None, "unitsConsumed": 45_000, "logs": []}
    plan = plan_sell(
        {
            "mint": MINT,
            "user": USER,
            "amount": AMOUNT,
            "min_sol_output": result.min_sol_output,
        },
        rpc_call=_fake_rpc(value, cashback=cashback),
    )
    declared = next(s for s in plan["landing_plan"] if s["kind"] == "sell")
    verified = next(s for s in result.landing_plan if s["kind"] == "sell")

    assert verified["accounts"] == declared["accounts"]
    prefix = declared["remaining_accounts"]
    assert verified["remaining_accounts"][: len(prefix)] == prefix

    recipe = declared["remaining_accounts_unresolved"]["buyback_fee_recipient"][
        "resolve"
    ]
    assert (
        recipe["read"],
        recipe["field_offset"],
        recipe["count"],
        recipe["take"],
    ) == (
        "global",
        741,
        8,
        0,
    )
    assert verified["remaining_accounts"] == sell_remaining_accounts(
        BONDING_CURVE_V2,
        BUYBACK[0],
        USER_VOLUME_ACCUMULATOR if cashback else None,
    )
    # the declared step also matches the plan's own account_count verdict
    assert plan["cashback"]["account_count"] == result.account_count


# --- the D2 corpus opt-in ----------------------------------------------------------


def test_record_to_default_none_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _run()
    assert list(tmp_path.iterdir()) == []


def test_record_to_appends_one_allowlisted_row_with_stable_hash(tmp_path) -> None:
    import json

    from gecko.corpus import SIMULATED_ALLOWED_KEYS

    corpus = tmp_path / "corpus.jsonl"
    value = {"err": None, "unitsConsumed": 45_000, "logs": ["Program success"]}
    other_user = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
    for user in (USER, other_user):
        simulate_sell_landing(
            {**_bindings(), "user": user},
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_fake_rpc(value),
            fetch_sell_instruction=lambda a, ar, fp: CANNED_SELL,
            include_derive_only=False,
            record_to=corpus,
        )
    sibling = tmp_path / "simulated.jsonl"
    assert sibling.exists()
    assert not corpus.exists()  # segregated: never the main (wire-only) corpus
    rows = [json.loads(line) for line in sibling.read_text().splitlines()]
    assert len(rows) == 2
    for row in rows:
        assert set(row) == SIMULATED_ALLOWED_KEYS
        assert row["status"] == "pass"
        assert row["instruction"] == "sell"
        assert row["source"] == "simulated"
    # same plan SHAPE → identical hash despite different resolved pubkeys
    assert rows[0]["recipe_hash"] == rows[1]["recipe_hash"]


def test_recorded_row_is_values_free(tmp_path) -> None:
    """The canary: a run whose sim response carries a pubkey, an amount and log lines
    must record a row containing NONE of them — nor any resolved value of its own."""
    import json

    canary_amount = 987_654_321
    canary_value = {
        "err": {"InstructionError": [0, {"Custom": 6062}], "leaked": USER},
        "unitsConsumed": 45_000,
        "logs": [f"Program log: sent {canary_amount} to {USER}", "Program failed"],
    }
    corpus = tmp_path / "corpus.jsonl"
    result = simulate_sell_landing(
        {**_bindings(), "amount": canary_amount},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(canary_value),
        fetch_sell_instruction=lambda a, ar, fp: CANNED_SELL,
        include_derive_only=False,
        record_to=corpus,
    )
    raw = (tmp_path / "simulated.jsonl").read_text()
    assert result.landing_receipt.status == "fail"
    for canary in (
        USER,
        MINT,
        BONDING_CURVE,
        FEE_RECIPIENT,
        BONDING_CURVE_V2,
        BUYBACK[0],
        str(canary_amount),
        "sent",
        "leaked",
        "Program log",
        "http://127.0.0.1:8899",
    ):
        assert canary not in raw
    row = json.loads(raw.strip())
    assert row["status"] == "fail"
    assert row["revert_class"] in {"account_error", "custom_program_error"}


# --- env-gated real E2E: the side-by-side a-ha on a surfpool mainnet fork -----------


def _anchor_custom_code(err: object) -> int | None:
    """The Anchor ``Custom`` code from a sim ``err``, if present — printed so the naive
    verdict line names the exact revert, never a fabricated number."""
    if isinstance(err, dict):
        instruction_error = err.get("InstructionError")
        if isinstance(instruction_error, list) and len(instruction_error) == 2:
            detail = instruction_error[1]
            if isinstance(detail, dict) and isinstance(detail.get("Custom"), int):
                return int(detail["Custom"])
    return None


def _find_a_real_holder(mint: str, *, rpc_url: str) -> tuple[str, int] | None:
    """A wallet that actually HOLDS ``mint``, discovered at run time.

    You cannot simulate a sell from a wallet with no tokens — the honest thing is to find
    a real holder rather than fake one. ``getTokenLargestAccounts`` gives token ACCOUNTS;
    each one's owner is read, and a holder is usable only if BOTH hold:

    * the owner is ON CURVE — a PDA cannot sign, and the largest holder of a
      pre-graduation pump token is always the ``bonding_curve`` itself (its ATA is
      ``associated_bonding_curve``), which would otherwise be picked first;
    * the token account IS the owner's canonical ATA — ``sell`` is planned against the
      derived ``associated_user``, so a non-ATA holding would plan the wrong account.

    Returns ``(owner, balance)`` or ``None`` — the caller SKIPs on None, never fabricates.
    """
    from solders.pubkey import Pubkey

    from gecko.pda import derive_pda
    from gecko.pda_resolve import read_account_owner
    from gecko.providers.pumpfun import _load_pumpfun_pdas
    from gecko.rpc import default_rpc_call

    resp = default_rpc_call(rpc_url, "getTokenLargestAccounts", [mint])
    accounts = (resp.get("result") or {}).get("value") or []
    pdas = _load_pumpfun_pdas()
    token_program = read_account_owner(mint, rpc_url=rpc_url)
    for entry in accounts:
        token_account = entry.get("address")
        balance = int(entry.get("amount") or 0)
        if not token_account or balance <= 0:
            continue
        info = default_rpc_call(
            rpc_url, "getAccountInfo", [token_account, {"encoding": "jsonParsed"}]
        )
        value = (info.get("result") or {}).get("value") or {}
        parsed = ((value.get("data") or {}).get("parsed") or {}).get("info") or {}
        owner = parsed.get("owner")
        if not isinstance(owner, str) or not Pubkey.from_string(owner).is_on_curve():
            continue
        ata = derive_pda(
            pdas["associated_user"],
            {"owner": owner, "token_program": token_program, "mint": mint},
        ).address
        if ata == token_account:
            return owner, balance
    return None


@pytest.mark.skipif(
    os.getenv("GECKO_SIMULATE_E2E") != "1",
    reason="set GECKO_SIMULATE_E2E=1 (+ surfpool + a mainnet RPC) to run the live a-ha",
)
def test_sell_that_passes_e2e_side_by_side() -> None:
    """Real Orquestra /build → assemble the landing bundle → simulate on a surfpool fork.
    The naive path is the builder's 14-account `sell` verbatim; the Gecko bundle adds the
    accounts that exist only in a doc-comment sentence. Prints the verbatim side-by-side."""
    from gecko.pda_resolve import read_account_field_pubkey
    from gecko.pda_testkit import SurfpoolError, SurfpoolFork

    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    mint = os.getenv("GECKO_E2E_SELL_MINT", MINT)
    try:
        with SurfpoolFork(mainnet) as fork:
            # A real sell needs a wallet that HOLDS the token. Discover one at run time;
            # if none is usable, SKIP with that exact reason rather than fake it.
            holder = _find_a_real_holder(mint, rpc_url=fork.rpc_url)
            if holder is None:
                pytest.skip(
                    f"no usable holder of {mint}: getTokenLargestAccounts returned no "
                    "account owned by an on-curve wallet whose canonical ATA is that "
                    "account — a sell cannot be simulated from a wallet that holds "
                    "nothing, and it will not be faked"
                )
            user, balance = holder
            # sell 1% of the holder's balance (a realistic partial exit, not a dust trade)
            amount = max(1, balance // 100)
            fee_recipient = os.getenv(
                "GECKO_E2E_FEE_RECIPIENT"
            ) or read_account_field_pubkey(GLOBAL, 162, rpc_url=fork.rpc_url)
            result = simulate_sell_landing(
                {
                    "mint": mint,
                    "user": user,
                    "amount": amount,
                    "fee_recipient": fee_recipient,
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

    naive_code = _anchor_custom_code(derive.err)
    naive_line = (
        "❌ NAIVE (builder's 14-account sell) — EXPECTED revert, this is the gap: "
        f"{derive.revert_class}{f' ({naive_code})' if naive_code is not None else ''}"
        if derive.status == "fail"
        else f"NAIVE (derive-only) — status={derive.status} (a revert was expected; see logs)"
    )
    gecko_line = (
        f"✅ GECKO landing bundle — PASSES: {land.units_consumed:,} CU"
        if land.status == "pass" and land.units_consumed is not None
        else f"GECKO landing bundle — status={land.status} revert_class={land.revert_class}"
    )
    print("\n=== pump sell: naive derive-only vs GECKO landing bundle ===")
    print(f"holder discovered at run time: {user} (balance {balance:,})")
    print(naive_line)
    print(gecko_line)
    print(
        "RESULT: the naive path reverts on mainnet; the Gecko bundle lands — "
        "caught for $0 before any spend."
    )

    print("--- details ---")
    shape = "cashback (17 accounts)" if result.is_cashback_coin else "non-cashback (16)"
    print(f"shape read from BondingCurve.is_cashback_coin@82: {shape}")
    print(
        f"amount={amount} base_sol_output={result.base_sol_output} "
        f"min_sol_output={result.min_sol_output} cu_limit={result.unit_limit}"
    )
    print(f"naive: status={derive.status} revert_class={derive.revert_class}")
    print(f"  logs_tail={list(derive.logs_tail)}")
    print(
        f"gecko landing bundle: status={land.status} units={land.units_consumed} "
        f"revert_class={land.revert_class}"
    )
    print(f"  logs_tail={list(land.logs_tail)}")
    print(f"network: {land.network_label}")

    # The differential is the thesis, and it is specific: the naive bundle reverts 6074
    # (InvalidBondingCurveV2) — the account the surface mentions only in an English
    # doc-comment — AFTER the token transfer already executed …
    assert derive.status == "fail"
    assert naive_code == 6074
    # … and the Gecko bundle, with the recovered appended set, lands.
    assert land.status == "pass"
