"""The claim-that-passes orchestrator (gecko/providers/ore_landing.py).

Offline (Pattern B, $0): an injected rpc_call serves the two state reads (a REAL mainnet
Miner blob + the Treasury singleton) and a canned simulateTransaction; an injected
fetch_claim_instruction stands in for Orquestra /build, returning the wire shape verified
LIVE — camelCase account names in the source's order, and ``data: "04"`` (the bare
discriminator) regardless of what args are sent. The bundle assembles ComputeBudget
around the built claimOre — and NOTHING else, because claim_ore creates the recipient
ATA itself — and simulates to status: pass.

The ORE-specific guard: a caller asking for a PARTIAL claim (bps < 10000) must not be
simulated at all, because the builder drops the argument and the resulting bundle would
claim 100%. A silent semantic swap is worse than a loud refusal; the test pins the
refusal. The real run happens in the env-gated E2E below, against a mainnet miner with
accrued rewards discovered at run time (or an honest skip).
"""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import pytest

from gecko.landing import ASSOCIATED_TOKEN_PROGRAM_ID, COMPUTE_BUDGET_PROGRAM_ID
from gecko.providers.ore import ORE_MINT, plan_claim
from gecko.providers.ore_landing import ClaimLandingError, simulate_claim_landing

from test_providers_ore import (  # the shared real-blob fixtures
    BOARD,
    CLAIM_SIGNER,
    MINER,
    ORE,
    REAL_MINER_BLOB,
    RECIPIENT,
    TREASURY_TOKENS,
    _claim_rpc,
)

# The account order + metas of the built `claimOre`, verified live against Orquestra
# /build AND against real mainnet instructions (11 accounts, signer writable).
_CLAIM_ACCOUNT_ORDER = [
    ("signer", True, True),
    ("board", False, False),  # WRONG on purpose — the builder's real (read-only) meta
    ("miner", False, True),
    ("mint", False, False),
    ("recipient", False, True),
    ("treasury", False, True),
    ("treasuryTokens", False, True),
    ("systemProgram", False, False),
    ("tokenProgram", False, False),
    ("associatedTokenProgram", False, False),
    ("oreProgram", False, False),
]

PASS_VALUE = {"err": None, "unitsConsumed": 24_000, "logs": ["Program success"]}


def _canned_claim(accounts: dict[str, str], data: str = "04") -> dict[str, Any]:
    """What /build really returns: data is the bare discriminator "04" — the optional
    `bps` arg is dropped whether or not it was sent (wire-verified 2026-08-05)."""
    return {
        "name": "claimOre",
        "programId": ORE,
        "data": data,
        "accounts": [
            {"pubkey": accounts[name], "isSigner": signer, "isWritable": writable}
            for name, signer, writable in _CLAIM_ACCOUNT_ORDER
        ],
    }


def _blob_with_authority(authority: str) -> str:
    """The real Miner blob with its `authority` field (struct offset 0 → account
    offset 8) rewritten — so a plan for a different signer clears the authority gate
    while every other byte stays real."""
    from solders.pubkey import Pubkey

    raw = bytearray(base64.b64decode(REAL_MINER_BLOB))
    raw[8:40] = bytes(Pubkey.from_string(authority))
    return base64.b64encode(bytes(raw)).decode()


def _fake_rpc(
    sim_value: dict[str, Any],
    sim_txs: list[str] | None = None,
    *,
    miner_authority: str | None = None,
):
    state_rpc = (
        _claim_rpc(_blob_with_authority(miner_authority))
        if miner_authority is not None
        else _claim_rpc()
    )

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "simulateTransaction":
            if sim_txs is not None:
                sim_txs.append(params[0])
            return {"result": {"value": sim_value}}
        return state_rpc(rpc_url, method, params)

    return rpc


def _is_writable(message: Any, index: int) -> bool:
    """Whether account `index` of a legacy message is writable — solders exposes only
    the header, so apply the wire rule: writable signers come first, then writable
    non-signers, with the read-only counts trailing each group."""
    header = message.header
    total = len(message.account_keys)
    signers = header.num_required_signatures
    if index < signers:
        return index < signers - header.num_readonly_signed_accounts
    return index < total - header.num_readonly_unsigned_accounts


def _instruction_programs(built_tx_b64: str) -> list[str]:
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(built_tx_b64))
    keys = [str(k) for k in tx.message.account_keys]
    return [keys[ci.program_id_index] for ci in tx.message.instructions]


def test_missing_bindings_raise() -> None:
    with pytest.raises(ClaimLandingError, match="signer"):
        simulate_claim_landing({}, rpc_call=_fake_rpc(PASS_VALUE))


def test_landing_bundle_simulates_to_pass_with_the_declared_claim_verdict() -> None:
    captured: dict[str, Any] = {}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        captured["accounts"] = dict(accounts)
        captured["args"] = dict(args)
        captured["fee_payer"] = fee_payer
        return _canned_claim(dict(accounts))

    result = simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_claim_instruction=fetch,
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    assert result.landing_receipt.status == "pass"
    assert result.landing_receipt.revert_class is None
    # the claim verdict rides along — declared at plan time, not discovered at revert
    assert result.authority == CLAIM_SIGNER
    assert result.claimable_ore == 283_645_877_668_295
    assert result.claim_amount_at_least == 266_594_620_078_012
    assert result.decimals == 11
    assert result.checkpoint_pending is False
    assert result.requested_bps == 10_000
    # claimOre takes NO amount: /build receives an empty args object
    assert captured["args"] == {}
    assert captured["fee_payer"] == CLAIM_SIGNER
    # CU limit from measured units (24_000 × 1.2)
    assert result.unit_limit == 28_800


def test_assembled_message_is_compute_budget_then_claim_with_no_ata() -> None:
    sim_txs: list[str] = []
    simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE, sim_txs),
        fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    programs = _instruction_programs(sim_txs[-1])  # the second (tight-limit) pass
    assert programs == [
        COMPUTE_BUDGET_PROGRAM_ID,  # SetComputeUnitLimit
        COMPUTE_BUDGET_PROGRAM_ID,  # SetComputeUnitPrice
        ORE,  # the claimOre — 11 accounts, nothing appended
    ]
    # NO idempotent-ATA prelude: the program creates the recipient itself (source)
    assert ASSOCIATED_TOKEN_PROGRAM_ID not in programs
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(sim_txs[-1]))
    claim_ci = [
        ci
        for ci in tx.message.instructions
        if str(tx.message.account_keys[ci.program_id_index]) == ORE
    ][0]
    assert len(claim_ci.accounts) == 11


def test_declared_plan_matches_orchestrator_assembly_no_drift() -> None:
    # The no-drift guard: expanding the DECLARED landing_plan kinds to their wire
    # programs must equal the program sequence of the tx the orchestrator actually
    # simulated. If either side changes alone, this fails.
    sim_txs: list[str] = []
    result = simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE, sim_txs),
        fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
        unit_price_microlamports=1_000,
        include_derive_only=False,
    )
    expansion = {
        "compute_budget": [COMPUTE_BUDGET_PROGRAM_ID],
        "claimOre": [ORE],
    }
    declared = [
        program for step in result.landing_plan for program in expansion[step["kind"]]
    ]
    assert declared == _instruction_programs(sim_txs[-1])


def test_declared_accounts_and_build_payload_do_not_drift() -> None:
    # plan_claim's DECLARED account set and the payload the orchestrator hands to
    # /build are the same truth — same names (the project's camelCase wire names),
    # same resolved addresses. If either side changes alone, this fails.
    captured: dict[str, Any] = {}

    def fetch(accounts: Any, args: Any, fee_payer: Any) -> Any:
        captured["accounts"] = dict(accounts)
        return _canned_claim(dict(accounts))

    simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_claim_instruction=fetch,
        include_derive_only=False,
    )
    declared = plan_claim({"signer": CLAIM_SIGNER}, rpc_call=_fake_rpc(PASS_VALUE))
    assert captured["accounts"] == declared["accounts"]
    assert captured["accounts"]["miner"] == MINER
    assert captured["accounts"]["mint"] == ORE_MINT
    assert captured["accounts"]["recipient"] == RECIPIENT
    assert captured["accounts"]["treasuryTokens"] == TREASURY_TOKENS


def test_partial_claim_is_refused_because_the_builder_drops_bps() -> None:
    """THE ORE semantics guard. /build returns data "04" even when bps is supplied, so
    a 50% ask would land as a 100% claim. Refuse loudly instead of simulating a lie."""
    with pytest.raises(ClaimLandingError, match="drops the optional `bps`"):
        simulate_claim_landing(
            {"signer": CLAIM_SIGNER, "bps": 5000},
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_fake_rpc(PASS_VALUE),
            fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
            include_derive_only=False,
        )
    # a builder that DOES carry the arg passes the same guard (source-true bytes)
    honest_data = "04" + struct.pack("<Q", 5000).hex()
    result = simulate_claim_landing(
        {"signer": CLAIM_SIGNER, "bps": 5000},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a), honest_data),
        include_derive_only=False,
    )
    assert result.landing_receipt.status == "pass"
    assert result.requested_bps == 5000
    assert result.claim_amount_at_least == 133_297_310_039_006  # half, source math


def test_gecko_corrects_the_read_only_board_the_builder_returns() -> None:
    """THE live-proven Class-1 fix. /build returns `board` with isMut: false (its
    surface says so). The deployed program passes the board into a closing self-CPI as
    a WRITABLE signer, so that instruction transfers the tokens and then dies on
    "writable privilege escalated". Gecko widens the meta from source before simulating
    — and only widens WRITABILITY, never signer-ness."""
    sim_txs: list[str] = []
    result = simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE, sim_txs),
        fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
        include_derive_only=False,
    )
    assert result.corrected_metas == (BOARD,)

    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(sim_txs[-1]))
    keys = [str(k) for k in tx.message.account_keys]
    assert _is_writable(tx.message, keys.index(BOARD))
    # signer-ness is untouched: the board never becomes a required signature
    assert not tx.message.is_signer(keys.index(BOARD))


def test_naive_receipt_is_the_builder_instruction_verbatim() -> None:
    # include_derive_only simulates the builder's instruction UNCORRECTED — board
    # read-only. The injected transport reverts exactly the tx whose board meta is
    # read-only, reproducing the live "writable privilege escalated" failure offline.
    from solders.transaction import Transaction

    ESCALATION = {
        "err": {"InstructionError": [2, "PrivilegeEscalation"]},
        "unitsConsumed": 40_187,
        "logs": [
            "Program log: Claiming 2799.06728745333 ORE.",
            "BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi's writable privilege escalated",
            "Program oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv failed: Cross-program "
            "invocation with unauthorized signer or writable account",
        ],
    }
    state_rpc = _claim_rpc()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "simulateTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            keys = [str(k) for k in tx.message.account_keys]
            board_writable = _is_writable(tx.message, keys.index(BOARD))
            return {"result": {"value": PASS_VALUE if board_writable else ESCALATION}}
        return state_rpc(rpc_url, method, params)

    result = simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=rpc,
        fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
        include_derive_only=True,
    )
    assert result.landing_receipt.status == "pass"  # Gecko's corrected metas land
    assert result.derive_only_receipt is not None
    assert result.derive_only_receipt.status == "fail"  # the builder's do not


def test_a_missing_instruction_in_the_build_response_raises() -> None:
    with pytest.raises(ClaimLandingError, match="data"):
        simulate_claim_landing(
            {"signer": CLAIM_SIGNER},
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_fake_rpc(PASS_VALUE),
            fetch_claim_instruction=lambda a, ar, fp: {"name": "claimOre"},
            include_derive_only=False,
        )


# --- the D2 corpus opt-in: record_to wiring on the orchestrator -----------------------


def test_record_to_default_none_writes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    simulate_claim_landing(
        {"signer": CLAIM_SIGNER},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_fake_rpc(PASS_VALUE),
        fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
        include_derive_only=False,
    )
    assert list(tmp_path.iterdir()) == []


def test_record_to_appends_one_values_free_row_with_stable_hash(tmp_path) -> None:
    import json

    from gecko.corpus import SIMULATED_ALLOWED_KEYS

    corpus = tmp_path / "corpus.jsonl"
    # two runs of the SAME intent with DIFFERENT resolved values: another authority →
    # a different miner, recipient and signer throughout (the blob's authority field is
    # rewritten so the wrong-authority gate is genuinely satisfied both times).
    other_signer = "AeRDs2bahLjCeyYg6166nmQyMabcGMjBQBvdWnzADfpe"
    for signer in (CLAIM_SIGNER, other_signer):
        simulate_claim_landing(
            {"signer": signer},
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_fake_rpc(PASS_VALUE, miner_authority=signer),
            fetch_claim_instruction=lambda a, ar, fp: _canned_claim(dict(a)),
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
        assert row["instruction"] == "claimOre"
        assert row["source"] == "simulated"
    # the drift key: same plan SHAPE → identical hash
    assert rows[0]["recipe_hash"] == rows[1]["recipe_hash"]
    # values-free: no resolved pubkey, balance, or RPC URL persisted
    for canary in (
        CLAIM_SIGNER,
        MINER,
        RECIPIENT,
        TREASURY_TOKENS,
        ORE_MINT,
        "283645877668295",
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


def _discover_claimable_miner(mainnet: str) -> tuple[str | None, str]:
    """Scan mainnet for a Miner account with a non-zero claimable ORE balance and
    return its AUTHORITY (the only key that can sign the claim). Control-plane reads
    only — a dataSlice over the two balance fields, then one authority read."""
    from gecko.ore_state import MINER_ACCOUNT_SIZE, OreStateError, decode_miner_state
    from gecko.rpc import default_rpc_call

    resp = default_rpc_call(
        mainnet,
        "getProgramAccounts",
        [
            "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv",
            {
                "encoding": "base64",
                # refined_ore + rewards_ore live at account offsets 696/704
                "dataSlice": {"offset": 696, "length": 16},
                "filters": [{"dataSize": MINER_ACCOUNT_SIZE}],
            },
        ],
    )
    entries = resp.get("result")
    if not isinstance(entries, list) or not entries:
        return None, "getProgramAccounts returned no miner accounts (RPC may block it)"
    best: tuple[int, str] | None = None
    for entry in entries:
        try:
            raw = base64.b64decode(entry["account"]["data"][0])
            refined, rewards = struct.unpack("<2Q", raw)
        except (KeyError, TypeError, ValueError, struct.error):
            continue
        if refined + rewards > 0 and (best is None or refined + rewards > best[0]):
            best = (refined + rewards, entry["pubkey"])
    if best is None:
        return None, f"none of {len(entries)} miner accounts holds claimable ORE"
    total, miner_address = best
    full = default_rpc_call(
        mainnet, "getAccountInfo", [miner_address, {"encoding": "base64"}]
    )
    value = (full.get("result") or {}).get("value")
    if not isinstance(value, dict):
        return None, f"miner {miner_address} vanished between reads"
    try:
        state = decode_miner_state(base64.b64decode(value["data"][0]))
    except OreStateError as exc:
        return None, f"miner {miner_address} did not decode: {exc}"
    return state.authority, f"miner {miner_address} holds {total} ORE base units"


@pytest.mark.skipif(
    os.getenv("GECKO_SIMULATE_E2E") != "1",
    reason="set GECKO_SIMULATE_E2E=1 (+ surfpool + a mainnet RPC) to run the live a-ha",
)
def test_claim_that_passes_e2e_side_by_side() -> None:
    """Real Orquestra /build → assemble the landing bundle → simulate on a surfpool
    fork, against a mainnet miner WITH accrued rewards discovered at run time
    (override with GECKO_E2E_ORE_SIGNER). A real claim needs a miner account that
    actually holds rewards — if none is findable, the test SKIPS with that exact
    reason and never fakes one. Prints the labeled side-by-side — the deliverable."""
    from gecko.pda_testkit import SurfpoolError, SurfpoolFork

    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    signer = os.getenv("GECKO_E2E_ORE_SIGNER")
    note = "signer supplied via GECKO_E2E_ORE_SIGNER"
    if not signer:
        signer, note = _discover_claimable_miner(mainnet)
        if signer is None:
            pytest.skip(f"no ORE miner with accrued rewards is claimable now: {note}")

    try:
        with SurfpoolFork(mainnet) as fork:
            result = simulate_claim_landing(
                {"signer": signer},
                rpc_url=fork.rpc_url,
                include_derive_only=True,
                network_label="surfpool fork (mainnet-backed — NOT mainnet)",
            )
    except SurfpoolError as exc:
        pytest.skip(f"surfpool fork unavailable: {exc}")

    derive = result.derive_only_receipt
    land = result.landing_receipt
    assert derive is not None

    # VERDICT FIRST, labels honest: the naive line reports what the builder's OWN
    # instruction did, and only claims a revert if one actually happened.
    naive_line = (
        f"❌ NAIVE (builder metas verbatim, board read-only) — FAILS: "
        f"{derive.revert_class}"
        if derive.status == "fail"
        else (
            "✅ NAIVE (builder metas verbatim) also lands — the builder agreed with "
            "source this time; the remaining claim gap is resolving mint / recipient / "
            "treasuryTokens at all (pda: null on the surface), reading the payout from "
            "a Miner layout the shipped IDL gets wrong, and the dropped bps arg"
        )
    )
    gecko_line = (
        f"✅ GECKO landing bundle — PASSES: {land.units_consumed:,} CU"
        if land.status == "pass" and land.units_consumed is not None
        else f"GECKO landing bundle — status={land.status} revert_class={land.revert_class}"
    )
    ore = 10**result.decimals
    print("\n=== ore claim: naive derive-only vs GECKO landing bundle ===")
    print(f"discovery: {note}")
    print(
        f"authority={result.authority} bps={result.requested_bps} "
        f"checkpoint_pending={result.checkpoint_pending}"
    )
    print(
        f"claimable={result.claimable_ore / ore:.6f} ORE "
        f"→ pays out at least {result.claim_amount_at_least / ore:.6f} ORE "
        f"(refining fee {result.refining_fee / ore:.6f}, {result.decimals} decimals)"
    )
    print(naive_line)
    print(gecko_line)
    print(
        "gecko corrected account metas from source: "
        + (", ".join(result.corrected_metas) or "none needed")
    )
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

    # the deliverable: the Gecko-complete bundle lands on a claimable miner.
    assert result.claimable_ore > 0
    assert land.status == "pass"
