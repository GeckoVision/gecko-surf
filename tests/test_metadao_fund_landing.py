"""The fund-that-passes orchestrator (gecko/providers/metadao_landing.py).

Offline (Pattern B, $0): an injected rpc_call serves the ONE launch-state read and a
canned simulateTransaction; an injected fetch_fund_instruction stands in for Orquestra
/build (returning the wire shape verified LIVE — camelCase account names, data =
fund discriminator + amount u64). The bundle assembles the funder-USDC-ATA prelude +
ComputeBudget around the built fund and simulates to status: pass; funding_record
needs NO prelude (the program init_if_needed's it — source-verified). The no-drift
guard proves the DECLARED landing_plan and the assembled unsigned message are the same
ordered contract. The real run happens in the env-gated E2E below, against a
currently-Live mainnet launch discovered at run time (or an honest skip).
"""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import pytest

from gecko.landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
)
from gecko.providers.metadao import plan_fund
from gecko.providers.metadao_landing import (
    FundLandingError,
    simulate_fund_landing,
)

from test_providers_metadao import (  # the shared canned-launch fixtures
    BASE_MINT2,
    EVENT_AUTHORITY,
    FUNDER,
    FUNDER_USDC_ATA,
    FUNDING_RECORD,
    LAUNCH2,
    LAUNCH2_QUOTE_VAULT,
    LAUNCHPAD,
    USDC,
    _fund_bindings,
    _live_launch_blob,
)

# The account order + metas of the built `fund`, verified live against Orquestra
# /build (which matches v07 fund.rs exactly): 10 accounts, funder = read-only signer,
# payer = writable signer.
_FUND_ACCOUNT_ORDER = [
    ("launch", False, True),
    ("fundingRecord", False, True),
    ("launchQuoteVault", False, True),
    ("funder", True, False),
    ("payer", True, True),
    ("funderQuoteAccount", False, True),
    ("tokenProgram", False, False),
    ("systemProgram", False, False),
    ("eventAuthority", False, False),
    ("program", False, False),
]


def _canned_fund(accounts: dict[str, str], amount: int) -> dict[str, Any]:
    # data = sha256("global:fund")[:8] + amount u64 LE — the exact bytes the live
    # /build returned for this project.
    return {
        "name": "fund",
        "programId": LAUNCHPAD,
        "data": "dabc6fdd9871ae07" + struct.pack("<Q", amount).hex(),
        "accounts": [
            {"pubkey": accounts[name], "isSigner": signer, "isWritable": writable}
            for name, signer, writable in _FUND_ACCOUNT_ORDER
        ],
    }


PASS_VALUE = {"err": None, "unitsConsumed": 30_000, "logs": ["Program success"]}


def _fake_rpc(sim_value: dict[str, Any], sim_txs: list[str] | None = None):
    blob = _live_launch_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            if params[0] == LAUNCH2:
                return {
                    "result": {"value": {"owner": LAUNCHPAD, "data": [blob, "base64"]}}
                }
            return {"result": {"value": {"lamports": 500_000_000}}}  # payer (track)
        if method == "simulateTransaction":
            if sim_txs is not None:
                sim_txs.append(params[0])
            return {"result": {"value": sim_value}}
        raise AssertionError(f"unexpected method {method}")

    return rpc


def _instruction_programs(built_tx_b64: str) -> list[str]:
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(built_tx_b64))
    keys = [str(k) for k in tx.message.account_keys]
    return [keys[ci.program_id_index] for ci in tx.message.instructions]


def test_missing_bindings_raise() -> None:
    with pytest.raises(FundLandingError, match="amount"):
        simulate_fund_landing(
            {"base_mint": BASE_MINT2, "funder": FUNDER},
            rpc_call=_fake_rpc(PASS_VALUE),
        )


def test_landing_bundle_simulates_to_pass_with_the_declared_window() -> None:
    captured: dict[str, Any] = {}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        captured["accounts"] = dict(accounts)
        captured["args"] = dict(args)
        captured["fee_payer"] = fee_payer
        return _canned_fund(dict(accounts), int(args["amount"]))

    result = simulate_fund_landing(
        _fund_bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_fund_instruction=fetch,
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    assert result.landing_receipt.status == "pass"
    assert result.landing_receipt.revert_class is None
    # the launch-state verdict rides along — declared, not discovered at revert time
    assert result.launch_state == "Live"
    assert result.fund_window_open is True
    # the amount (USDC base units) reaches /build as a plain u64
    assert captured["args"] == {"amount": 10_000_000}
    assert captured["fee_payer"] == FUNDER
    # CU limit from measured units (30_000 × 1.2)
    assert result.unit_limit == 36_000


def test_assembled_message_orders_ata_then_fund() -> None:
    sim_txs: list[str] = []
    simulate_fund_landing(
        _fund_bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE, sim_txs),
        fetch_fund_instruction=lambda a, ar, fp: _canned_fund(
            dict(a), int(ar["amount"])
        ),
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    programs = _instruction_programs(sim_txs[-1])  # the second (tight-limit) pass
    assert programs == [
        COMPUTE_BUDGET_PROGRAM_ID,  # SetComputeUnitLimit
        COMPUTE_BUDGET_PROGRAM_ID,  # SetComputeUnitPrice
        ASSOCIATED_TOKEN_PROGRAM_ID,  # funder USDC ATA (idempotent)
        LAUNCHPAD,  # the fund — 10 accounts, nothing appended
    ]
    # the fund instruction carries exactly the 10 verified accounts — no hidden set
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(sim_txs[-1]))
    fund_ci = [
        ci
        for ci in tx.message.instructions
        if str(tx.message.account_keys[ci.program_id_index]) == LAUNCHPAD
    ][0]
    assert len(fund_ci.accounts) == 10


def test_declared_plan_matches_orchestrator_assembly_no_drift() -> None:
    # The no-drift guard: expanding the DECLARED landing_plan kinds to their wire
    # programs must equal the program sequence of the tx the orchestrator actually
    # simulated. If either side changes alone, this fails.
    sim_txs: list[str] = []
    result = simulate_fund_landing(
        _fund_bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE, sim_txs),
        fetch_fund_instruction=lambda a, ar, fp: _canned_fund(
            dict(a), int(ar["amount"])
        ),
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    expansion = {
        "compute_budget": [COMPUTE_BUDGET_PROGRAM_ID],
        "create_idempotent_ata": [ASSOCIATED_TOKEN_PROGRAM_ID],
        "fund": [LAUNCHPAD],
    }
    declared = [
        program for step in result.landing_plan for program in expansion[step["kind"]]
    ]
    assert declared == _instruction_programs(sim_txs[-1])


def test_declared_accounts_and_build_payload_do_not_drift() -> None:
    # plan_fund's DECLARED account set and the payload the orchestrator hands to
    # /build are the same truth — same names (the project's camelCase wire names),
    # same resolved addresses. If either side changes alone, this fails.
    captured: dict[str, Any] = {}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        captured["accounts"] = dict(accounts)
        return _canned_fund(dict(accounts), int(args["amount"]))

    simulate_fund_landing(
        _fund_bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_fund_instruction=fetch,
        include_derive_only=False,
    )
    declared = plan_fund(_fund_bindings(), rpc_call=_fake_rpc(PASS_VALUE))
    assert captured["accounts"] == declared["accounts"]
    assert captured["accounts"]["fundingRecord"] == FUNDING_RECORD
    assert captured["accounts"]["launchQuoteVault"] == LAUNCH2_QUOTE_VAULT
    assert captured["accounts"]["funderQuoteAccount"] == FUNDER_USDC_ATA
    assert captured["accounts"]["eventAuthority"] == EVENT_AUTHORITY


def test_derive_only_receipt_is_the_raw_build() -> None:
    # include_derive_only simulates the RAW built fund too (no ATA prelude): the
    # fake reverts any tx whose message lacks the ATA program — the funder-without-
    # an-ATA world, where only the Gecko-complete bundle lands.
    blob = _live_launch_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            if params[0] == LAUNCH2:
                return {
                    "result": {"value": {"owner": LAUNCHPAD, "data": [blob, "base64"]}}
                }
            return {"result": {"value": {"lamports": 500_000_000}}}
        if method == "simulateTransaction":
            if ASSOCIATED_TOKEN_PROGRAM_ID in _instruction_programs(params[0]):
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
        raise AssertionError(f"unexpected method {method}")

    result = simulate_fund_landing(
        _fund_bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        fetch_fund_instruction=lambda a, ar, fp: _canned_fund(
            dict(a), int(ar["amount"])
        ),
        include_derive_only=True,
    )
    assert result.landing_receipt.status == "pass"
    assert result.derive_only_receipt is not None
    assert result.derive_only_receipt.status == "fail"
    assert result.derive_only_receipt.revert_class == "account_error"


# --- the D2 corpus opt-in: record_to wiring on the orchestrator -----------------------


def test_record_to_default_none_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    simulate_fund_landing(
        _fund_bindings(),
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_fund_instruction=lambda a, ar, fp: _canned_fund(
            dict(a), int(ar["amount"])
        ),
        include_derive_only=False,
    )
    assert list(tmp_path.iterdir()) == []


def test_record_to_appends_one_values_free_row_with_stable_hash(tmp_path) -> None:
    import json

    from gecko.corpus import SIMULATED_ALLOWED_KEYS

    corpus = tmp_path / "corpus.jsonl"
    canary_amount = 987_654_321
    # two runs of the SAME intent with DIFFERENT resolved values (another funder →
    # different funding_record + USDC ATA throughout)
    other_funder = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
    for funder in (FUNDER, other_funder):
        simulate_fund_landing(
            {**_fund_bindings(), "funder": funder, "amount": canary_amount},
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_fake_rpc(PASS_VALUE),
            fetch_fund_instruction=lambda a, ar, fp: _canned_fund(
                dict(a), int(ar["amount"])
            ),
            include_derive_only=False,
            record_to=corpus,
        )
    sibling = tmp_path / "simulated.jsonl"
    assert sibling.exists()
    assert not corpus.exists()  # segregated: never the main (wire-only) corpus
    raw = sibling.read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 2  # exactly one row per opted-in run
    for row in rows:
        assert set(row) == SIMULATED_ALLOWED_KEYS
        assert row["status"] == "pass"
        assert row["instruction"] == "fund"
        assert row["source"] == "simulated"
    # the drift key: same plan SHAPE → identical hash despite different funders
    assert rows[0]["recipe_hash"] == rows[1]["recipe_hash"]
    # values-free: no resolved pubkey, amount, or RPC URL persisted
    for canary in (
        FUNDER,
        other_funder,
        LAUNCH2,
        LAUNCH2_QUOTE_VAULT,
        FUNDER_USDC_ATA,
        str(canary_amount),
        "http://127.0.0.1:8899",
    ):
        assert canary not in raw


# --- env-gated real E2E: the side-by-side on a surfpool mainnet fork ------------------


def _b58encode(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = alphabet[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def _discover_live_launch_and_funder(
    mainnet: str, amount: int
) -> tuple[str | None, str | None, str]:
    """Scan mainnet for a Launch in state Live with an open fund window, then a
    funder of that launch whose USDC ATA still holds >= amount. Control-plane reads
    only; returns (base_mint, funder, note)."""
    import time

    from solders.pubkey import Pubkey

    from gecko.metadao_state import MetadaoStateError, decode_launch_state
    from gecko.pda import derive_pda
    from gecko.providers.metadao import _load_metadao_pdas
    from gecko.rpc import default_rpc_call

    resp = default_rpc_call(
        mainnet,
        "getProgramAccounts",
        [LAUNCHPAD, {"encoding": "base64"}],
    )
    entries = resp.get("result") or []
    funding_disc = bytes.fromhex(
        "1422fbeecc750b43"
    )  # sha256("account:FundingRecord")[:8]

    best: tuple[int, str, str] | None = None  # (total_committed, base_mint, launch)
    records: dict[str, list[str]] = {}  # launch address -> funder pubkeys
    now = int(time.time())
    for entry in entries:
        raw = base64.b64decode(entry["account"]["data"][0])
        if raw[:8] == funding_disc:
            launch_addr = _b58encode(raw[41:73])
            records.setdefault(launch_addr, []).append(_b58encode(raw[9:41]))
            continue
        try:
            state = decode_launch_state(raw)
        except MetadaoStateError:
            continue
        if state.fund_window(now)[0] and state.quote_mint == USDC:
            key = (state.total_committed_amount, state.base_mint, entry["pubkey"])
            if best is None or key > best:
                best = key
    if best is None:
        return None, None, "no launch is Live with an open fund window"
    _, base_mint, launch_addr = best

    pdas = _load_metadao_pdas()
    token_program = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    for funder in records.get(launch_addr, [])[:25]:
        ata = derive_pda(
            pdas["funder_quote_account"],
            {"owner": funder, "token_program": token_program, "mint": USDC},
        ).address
        balance = default_rpc_call(mainnet, "getTokenAccountBalance", [ata])
        value = (balance.get("result") or {}).get("value") or {}
        try:
            if int(value.get("amount", 0)) >= amount:
                return base_mint, funder, f"live launch {launch_addr}"
        except (TypeError, ValueError):
            continue
    _ = Pubkey  # keep the solders import honest for type checkers
    return (
        base_mint,
        None,
        (
            f"live launch {launch_addr} found, but none of its first "
            f"{len(records.get(launch_addr, [])[:25])} funders still holds "
            f">= {amount} USDC base units in their ATA"
        ),
    )


@pytest.mark.skipif(
    os.getenv("GECKO_SIMULATE_E2E") != "1",
    reason="set GECKO_SIMULATE_E2E=1 (+ surfpool + a mainnet RPC) to run the live a-ha",
)
def test_fund_that_passes_e2e_side_by_side() -> None:
    """Real Orquestra /build → assemble the landing bundle → simulate on a surfpool
    fork, against a CURRENTLY-LIVE mainnet launch discovered at run time (override
    with GECKO_E2E_METADAO_BASE_MINT / GECKO_E2E_METADAO_FUNDER). If nothing is
    fundable right now, the test SKIPS with that exact reason — never fakes.
    Prints the labeled side-by-side — this is the deliverable."""
    from gecko.pda_testkit import (
        start_failure_is_a_broken_gate,
        SurfpoolError,
        SurfpoolFork,
    )

    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    amount = int(os.getenv("GECKO_E2E_METADAO_AMOUNT", "10000"))  # 0.01 USDC
    base_mint = os.getenv("GECKO_E2E_METADAO_BASE_MINT")
    funder = os.getenv("GECKO_E2E_METADAO_FUNDER")
    if not (base_mint and funder):
        found_mint, found_funder, note = _discover_live_launch_and_funder(
            mainnet, amount
        )
        base_mint = base_mint or found_mint
        funder = funder or found_funder
        if base_mint is None:
            pytest.skip(f"no fundable MetaDAO launch on mainnet right now: {note}")
        if funder is None:
            pytest.skip(f"no USDC-holding funder found: {note}")

    try:
        with SurfpoolFork(mainnet) as fork:
            result = simulate_fund_landing(
                {"base_mint": base_mint, "funder": funder, "amount": amount},
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
    assert derive is not None

    # VERDICT FIRST, labels honest: for THIS program the Class-1 gap is upstream
    # (the IDL carries ZERO PDA seeds; the vault is a state field) — a funder who
    # already holds the USDC ATA can land even derive-only, and the label must not
    # fake a revert that did not happen.
    naive_line = (
        f"❌ NAIVE (derive-only, no ATA prelude) — reverts: {derive.revert_class}"
        if derive.status == "fail"
        else (
            "✅ NAIVE (derive-only) also lands — this funder already holds the USDC "
            "ATA; the fund gap is deriving/reading the account set at all (the IDL "
            "carries ZERO PDA seeds), not the prelude"
        )
    )
    gecko_line = (
        f"✅ GECKO landing bundle — PASSES: {land.units_consumed:,} CU"
        if land.status == "pass" and land.units_consumed is not None
        else f"GECKO landing bundle — status={land.status} revert_class={land.revert_class}"
    )
    print("\n=== metadao fund: naive derive-only vs GECKO landing bundle ===")
    print(
        f"launch state={result.launch_state} window_open={result.fund_window_open} "
        f"amount={amount} (USDC base units)"
    )
    print(f"  {result.fund_window_note}")
    print(naive_line)
    print(gecko_line)
    print("--- details ---")
    print(f"cu_limit={result.unit_limit}")
    print(f"naive: status={derive.status} revert_class={derive.revert_class}")
    print(f"  logs_tail={list(derive.logs_tail)}")
    print(
        f"gecko: status={land.status} units={land.units_consumed} "
        f"revert_class={land.revert_class}"
    )
    print(f"  logs_tail={list(land.logs_tail)}")
    print(f"network: {land.network_label}")

    # the deliverable: the Gecko-complete bundle lands on a fundable launch.
    assert result.fund_window_open
    assert land.status == "pass"
