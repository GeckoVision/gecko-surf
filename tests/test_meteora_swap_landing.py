"""The swap-that-passes orchestrator (gecko/providers/meteora_landing.py).

Offline (Pattern B, $0): an injected rpc_call serves the pool-state read and a canned
err:null simulateTransaction; an injected fetch_swap_instruction stands in for Orquestra
/build (returning the wire shape observed LIVE — including the empty-pubkey OPTIONAL
slots). The bundle assembles ATAs + wSOL wrap around the built swap, appends the
bitmap-selected bin_array remaining accounts, closes the wSOL ATA after, and simulates
to status: pass — with the state-quoted min_amount_out, not a guess. The no-drift guard
proves the DECLARED landing_plan and the assembled unsigned message are the same
ordered contract. The real differential runs in the env-gated E2E below.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest

from gecko.landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from gecko.providers.meteora_landing import (
    SwapLandingError,
    _fill_optional_account_slots,
    simulate_swap_landing,
)

from test_providers_meteora import (  # the shared canned-pool fixtures
    BIN_ARRAY_0,
    BIN_ARRAY_M1,
    BIN_ARRAY_P1,
    CURRENT_POOL,
    METEORA_PROGRAM_ID,
    TOKEN,
    USER,
    WSOL,
    _bindings,
    _lb_pair_blob,
)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


# A canned Orquestra `/build` swap instruction in the LIVE-observed wire shape:
# 15 positional accounts with the two omitted OPTIONALS as EMPTY pubkeys (slots 1 and
# 9) — the wire finding this module normalizes. Data = swap discriminator
# f8c69e91e17587c8 + amount_in u64 + min_amount_out u64.
def _canned_swap(accounts: dict[str, str]) -> dict[str, Any]:
    order = [
        ("lb_pair", False, True),
        ("", False, True),  # bin_array_bitmap_extension — omitted optional
        ("reserve_x", False, True),
        ("reserve_y", False, True),
        ("user_token_in", False, True),
        ("user_token_out", False, True),
        ("token_x_mint", False, False),
        ("token_y_mint", False, False),
        ("oracle", False, True),
        ("", False, True),  # host_fee_in — omitted optional
        ("user", True, False),
        ("token_x_program", False, False),
        ("token_y_program", False, False),
        ("event_authority", False, False),
        ("program", False, False),
    ]
    return {
        "name": "swap",
        "programId": METEORA_PROGRAM_ID,
        "data": "f8c69e91e17587c8" + "40420f0000000000" + "0100000000000000",
        "accounts": [
            {
                "pubkey": accounts[name] if name else "",
                "isSigner": signer,
                "isWritable": writable,
            }
            for name, signer, writable in order
        ],
    }


def _fake_rpc(sim_value: dict[str, Any]):
    blob = _lb_pair_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            addr = params[0]
            if addr == CURRENT_POOL:
                return {"result": {"value": {"owner": "x", "data": [blob, "base64"]}}}
            if addr in (TOKEN, WSOL):
                return {"result": {"value": {"owner": TOKEN_PROGRAM}}}
            return {"result": {"value": {"lamports": 500_000_000}}}  # user (track)
        if method == "simulateTransaction":
            return {"result": {"value": sim_value}}
        raise AssertionError(f"unexpected method {method}")

    return rpc


def _instruction_programs(built_tx_b64: str) -> list[str]:
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(built_tx_b64))
    keys = [str(k) for k in tx.message.account_keys]
    return [keys[ci.program_id_index] for ci in tx.message.instructions]


PASS_VALUE = {"err": None, "unitsConsumed": 120_000, "logs": ["Program success"]}


def test_missing_bindings_raise() -> None:
    with pytest.raises(SwapLandingError, match="amount_in"):
        simulate_swap_landing(
            {k: v for k, v in _bindings().items() if k != "amount_in"},
            rpc_call=_fake_rpc(PASS_VALUE),
        )


def test_fill_optional_account_slots_uses_program_id_placeholder() -> None:
    # The live wire finding: /build returns omitted optional accounts as "" pubkeys.
    # Anchor's omitted-optional convention is the program's own id in the slot —
    # a public constant, never a fabrication, and the positional list stays intact.
    built = _canned_swap({**{k: USER for k in _names()}, "program": METEORA_PROGRAM_ID})
    filled = _fill_optional_account_slots(built, METEORA_PROGRAM_ID)
    slots = [a["pubkey"] for a in filled["accounts"]]
    assert slots[1] == METEORA_PROGRAM_ID and slots[9] == METEORA_PROGRAM_ID
    assert "" not in slots
    assert len(slots) == 15  # positional — nothing dropped


def _names() -> list[str]:
    return [
        "lb_pair",
        "reserve_x",
        "reserve_y",
        "user_token_in",
        "user_token_out",
        "token_x_mint",
        "token_y_mint",
        "oracle",
        "user",
        "token_x_program",
        "token_y_program",
        "event_authority",
        "program",
    ]


def test_landing_bundle_simulates_to_pass_with_state_quoted_guard() -> None:
    captured: dict[str, Any] = {}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        captured["accounts"] = dict(accounts)
        captured["args"] = dict(args)
        return _canned_swap(dict(accounts))

    result = simulate_swap_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_swap_instruction=fetch,
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    assert result.landing_receipt.status == "pass"
    assert result.landing_receipt.revert_class is None
    # min_amount_out is STATE-QUOTED (snapshot − 5%), not guessed, and reaches /build
    assert captured["args"]["min_amount_out"] == result.min_amount_out
    assert 0 < result.min_amount_out < result.expected_out_snapshot
    # the Class-1 recovery: the bitmap-selected arrays, skipping the missing −2
    assert result.bin_array_indexes == [-1, 0, 1]
    assert result.bin_arrays == [BIN_ARRAY_M1, BIN_ARRAY_0, BIN_ARRAY_P1]
    # CU limit from measured units (120_000 × 1.2)
    assert result.unit_limit == 144_000


def test_assembled_message_orders_wrap_swap_close() -> None:
    # Capture the simulated tx and prove the wire order: compute-budget, both ATAs,
    # the wSOL wrap (System transfer + SyncNative), the swap, then CloseAccount.
    sim_txs: list[str] = []
    blob = _lb_pair_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            addr = params[0]
            if addr == CURRENT_POOL:
                return {"result": {"value": {"owner": "x", "data": [blob, "base64"]}}}
            if addr in (TOKEN, WSOL):
                return {"result": {"value": {"owner": TOKEN_PROGRAM}}}
            return {"result": {"value": {"lamports": 500_000_000}}}
        if method == "simulateTransaction":
            sim_txs.append(params[0])
            return {"result": {"value": PASS_VALUE}}
        raise AssertionError(f"unexpected method {method}")

    result = simulate_swap_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        fetch_swap_instruction=lambda a, ar, fp: _canned_swap(dict(a)),
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    programs = _instruction_programs(sim_txs[-1])  # the second (tight-limit) pass
    assert programs == [
        COMPUTE_BUDGET_PROGRAM_ID,  # SetComputeUnitLimit
        COMPUTE_BUDGET_PROGRAM_ID,  # SetComputeUnitPrice
        ASSOCIATED_TOKEN_PROGRAM_ID,  # user_token_in ATA (idempotent)
        ASSOCIATED_TOKEN_PROGRAM_ID,  # user_token_out ATA (idempotent)
        SYSTEM_PROGRAM_ID,  # wrap: transfer amount_in lamports
        TOKEN_PROGRAM_ID,  # wrap: SyncNative
        METEORA_PROGRAM_ID,  # the swap (bin_arrays appended)
        TOKEN_PROGRAM_ID,  # CloseAccount — unwrap the wSOL leg
    ]

    # the swap instruction carries the 15 named/positional accounts + 3 bin_arrays
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(sim_txs[-1]))
    swap_ci = [
        ci
        for ci in tx.message.instructions
        if str(tx.message.account_keys[ci.program_id_index]) == METEORA_PROGRAM_ID
    ][0]
    assert len(swap_ci.accounts) == 15 + 3
    keys = [str(k) for k in tx.message.account_keys]
    tail = [keys[i] for i in swap_ci.accounts[-3:]]
    assert tail == [BIN_ARRAY_M1, BIN_ARRAY_0, BIN_ARRAY_P1]
    assert result.landing_receipt.status == "pass"


def test_declared_plan_matches_orchestrator_assembly_no_drift() -> None:
    # The no-drift guard: expanding the DECLARED landing_plan kinds to their wire
    # programs must equal the program sequence of the tx the orchestrator actually
    # simulated. If either side changes alone, this fails.
    sim_txs: list[str] = []
    blob = _lb_pair_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            addr = params[0]
            if addr == CURRENT_POOL:
                return {"result": {"value": {"owner": "x", "data": [blob, "base64"]}}}
            if addr in (TOKEN, WSOL):
                return {"result": {"value": {"owner": TOKEN_PROGRAM}}}
            return {"result": {"value": {"lamports": 500_000_000}}}
        if method == "simulateTransaction":
            sim_txs.append(params[0])
            return {"result": {"value": PASS_VALUE}}
        raise AssertionError(f"unexpected method {method}")

    result = simulate_swap_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        fetch_swap_instruction=lambda a, ar, fp: _canned_swap(dict(a)),
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    expansion = {
        "compute_budget": [COMPUTE_BUDGET_PROGRAM_ID],
        "create_idempotent_ata": [ASSOCIATED_TOKEN_PROGRAM_ID],
        "wrap_sol": [SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID],
        "swap": [METEORA_PROGRAM_ID],
        "close_wsol_ata": [TOKEN_PROGRAM_ID],
    }
    declared = [
        program for step in result.landing_plan for program in expansion[step["kind"]]
    ]
    assert declared == _instruction_programs(sim_txs[-1])


def test_derive_only_receipt_is_the_raw_build() -> None:
    # include_derive_only simulates the RAW built swap too (no preludes, no
    # bin_arrays): the fake flags any tx whose message lacks the ATA program.
    def rpc_router(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "simulateTransaction":
            programs = _instruction_programs(params[0])
            if ASSOCIATED_TOKEN_PROGRAM_ID in programs:
                return {"result": {"value": PASS_VALUE}}
            return {
                "result": {
                    "value": {
                        "err": {"InstructionError": [0, {"Custom": 3012}]},
                        "unitsConsumed": 5_000,
                        "logs": [
                            "Error Code: AccountNotInitialized. Error Number: 3012."
                        ],
                    }
                }
            }
        return _fake_rpc(PASS_VALUE)(rpc_url, method, params)

    result = simulate_swap_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc_router,
        fetch_swap_instruction=lambda a, ar, fp: _canned_swap(dict(a)),
        include_derive_only=True,
    )
    assert result.landing_receipt.status == "pass"
    assert result.derive_only_receipt is not None
    assert result.derive_only_receipt.status == "fail"
    assert result.derive_only_receipt.revert_class == "account_error"


# --- the D2 corpus opt-in (task #85): record_to wiring on the orchestrator ------------


def test_record_to_default_none_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    simulate_swap_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_swap_instruction=lambda a, ar, fp: _canned_swap(dict(a)),
        include_derive_only=False,
    )
    assert list(tmp_path.iterdir()) == []


def test_record_to_appends_one_categorical_swap_row(tmp_path) -> None:
    import json

    from gecko.corpus import SIMULATED_ALLOWED_KEYS
    from gecko.providers.meteora import METEORA_PROGRAM_ID as PROGRAM

    corpus = tmp_path / "corpus.jsonl"
    result = simulate_swap_landing(
        _bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_swap_instruction=lambda a, ar, fp: _canned_swap(dict(a)),
        include_derive_only=False,
        record_to=corpus,
    )
    sibling = tmp_path / "simulated.jsonl"
    raw = sibling.read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 1  # exactly one row for the opted-in run
    row = rows[0]
    assert set(row) == SIMULATED_ALLOWED_KEYS
    assert row["instruction"] == "swap"
    assert row["program_id"] == PROGRAM
    assert row["status"] == "pass"
    assert row["source"] == "simulated"
    # values-free: no resolved account or bin_array pubkey persisted (the amount
    # canary is covered digit-collision-safely in the pump orchestrator canary test)
    assert result.min_amount_out > 0
    for canary in (USER, CURRENT_POOL, *result.bin_arrays):
        assert canary not in raw


@pytest.mark.skipif(
    os.getenv("GECKO_SIMULATE_E2E") != "1",
    reason="set GECKO_SIMULATE_E2E=1 (+ surfpool + a mainnet RPC) to run the live a-ha",
)
def test_swap_that_passes_e2e_side_by_side() -> None:
    """Real Orquestra /build → assemble the landing bundle → simulate on a surfpool
    fork, against the deep SOL/USDC pool (bin_step 10, base_factor 10000 — a REAL
    4-seed pool). The derive-only path reverts; the Gecko-complete landing bundle
    (ATAs + wSOL wrap + bin_arrays + state-quoted min_amount_out + close) passes.
    Prints the verbatim side-by-side — this is the deliverable."""
    from gecko.pda_testkit import (
        start_failure_is_a_broken_gate,
        SurfpoolError,
        SurfpoolFork,
    )

    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    user = os.getenv("GECKO_E2E_USER", USER)
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    bindings = {
        "input_mint": WSOL,  # native SOL leg → exercises wrap + close
        "output_mint": usdc,
        "bin_step": int(os.getenv("GECKO_E2E_METEORA_BIN_STEP", "10")),
        "base_factor": int(os.getenv("GECKO_E2E_METEORA_BASE_FACTOR", "10000")),
        "user": user,
        "amount_in": 1_000_000,  # 0.001 SOL
    }
    try:
        with SurfpoolFork(mainnet) as fork:
            result = simulate_swap_landing(
                bindings,
                rpc_url=fork.rpc_url,
                include_derive_only=True,
                network_label="surfpool fork (mainnet-backed — NOT mainnet)",
            )
    except SurfpoolError as exc:
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"surfpool fork unavailable: {exc}")

    derive = result.derive_only_receipt
    land = result.landing_receipt
    print("\n=== meteora swap: derive-only vs Gecko-complete landing bundle ===")
    print(
        f"expected_out={result.expected_out_snapshot} "
        f"min_amount_out={result.min_amount_out} cu_limit={result.unit_limit}"
    )
    print(f"bin_arrays={result.bin_array_indexes} -> {result.bin_arrays}")
    print(
        f"DERIVE-ONLY (no ATAs/wrap/bin_arrays): status={derive.status if derive else None} "
        f"revert_class={derive.revert_class if derive else None}"
    )
    print(f"  logs_tail={list(derive.logs_tail) if derive else None}")
    print(
        f"GECKO-COMPLETE (landing bundle): status={land.status} "
        f"units={land.units_consumed} revert_class={land.revert_class}"
    )
    print(f"  logs_tail={list(land.logs_tail)}")

    # the differential is the thesis: derive-only fails at the first landing gap …
    assert derive is not None
    assert derive.status == "fail"
    # … and the assembled landing bundle lands.
    assert land.status == "pass"
