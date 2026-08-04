"""The landing preludes + bundle assembly (gecko/landing.py), offline-falsifiable.

Every path is proven with solders (the dev [solana] extra) + an injected rpc_call — no
network, no signing. The BOUNDARY test structurally asserts no sign/send/broadcast path
exists anywhere in the landing layer: assembling standard instructions to PROVE a bundle
lands is verification, not signing.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from gecko.landing import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    COMPUTE_BUDGET_PROGRAM_ID,
    LandingError,
    assemble_unsigned_tx,
    compute_budget_ixs,
    create_idempotent_ata_ix,
    orquestra_instruction_to_solders,
    simulate_landing_bundle,
)

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
USER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
ASSOCIATED_USER = "CTAUKpZkmejuonDJnBRW43FMZx6WpkytQrF8Cty4GfVc"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"

# A canned Orquestra `/build` buy instruction (data is hex; disc 0x66… is pump `buy`).
CANNED_BUY = {
    "name": "buy",
    "programId": PUMP,
    "data": "66063d1201daebea40420f000000000080f0fa020000000001",
    "accounts": [
        {"pubkey": GLOBAL, "isSigner": False, "isWritable": False},
        {"pubkey": USER, "isSigner": True, "isWritable": True},
    ],
}


def _instruction_programs(built_tx_b64: str) -> list[str]:
    """Deserialize the assembled unsigned tx and list its instruction program ids in order."""
    from solders.transaction import Transaction

    tx = Transaction.from_bytes(base64.b64decode(built_tx_b64))
    keys = [str(k) for k in tx.message.account_keys]
    return [keys[ci.program_id_index] for ci in tx.message.instructions]


# --- prelude builders --------------------------------------------------------


def test_compute_budget_ixs_limit_only() -> None:
    ixs = compute_budget_ixs(200_000)
    assert len(ixs) == 1
    assert bytes(ixs[0].data)[0] == 2  # SetComputeUnitLimit


def test_compute_budget_ixs_with_price() -> None:
    ixs = compute_budget_ixs(200_000, 1_000)
    assert len(ixs) == 2
    assert bytes(ixs[1].data)[0] == 3  # SetComputeUnitPrice


def test_create_idempotent_ata_ix_shape() -> None:
    ix = create_idempotent_ata_ix(USER, USER, MINT, ASSOCIATED_USER, TOKEN_2022)
    assert str(ix.program_id) == ASSOCIATED_TOKEN_PROGRAM_ID
    assert bytes(ix.data) == bytes([1])  # CreateIdempotent
    assert len(ix.accounts) == 6
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable  # payer


def test_orquestra_instruction_to_solders_parses_hex_and_metas() -> None:
    ix = orquestra_instruction_to_solders(CANNED_BUY)
    assert str(ix.program_id) == PUMP
    assert bytes(ix.data).hex() == CANNED_BUY["data"]
    assert str(ix.accounts[1].pubkey) == USER
    assert ix.accounts[1].is_signer and ix.accounts[1].is_writable


def test_orquestra_instruction_bad_hex_raises() -> None:
    with pytest.raises(LandingError):
        orquestra_instruction_to_solders({**CANNED_BUY, "data": "nothex!"})


def test_orquestra_instruction_missing_fields_raises() -> None:
    with pytest.raises(LandingError):
        orquestra_instruction_to_solders({"programId": PUMP})


# --- assembly: the prelude is really in the bundle ---------------------------


def test_assemble_bundle_contains_ata_prelude_before_buy() -> None:
    buy_ix = orquestra_instruction_to_solders(CANNED_BUY)
    ata_ix = create_idempotent_ata_ix(USER, USER, MINT, ASSOCIATED_USER, TOKEN_2022)
    built = assemble_unsigned_tx(
        [*compute_budget_ixs(200_000, 1_000), ata_ix, buy_ix], USER
    )
    assert built.encoding == "base64"
    programs = _instruction_programs(built.tx)
    assert ASSOCIATED_TOKEN_PROGRAM_ID in programs  # the create-idempotent-ATA prelude
    assert COMPUTE_BUDGET_PROGRAM_ID in programs
    # ordered: compute-budget…, ATA, then the buy — the ATA lands before the buy uses it
    assert programs.index(ASSOCIATED_TOKEN_PROGRAM_ID) < programs.index(PUMP)


# --- two-pass simulate: the a-ha (pass) and the differential (3012) ----------


def _sim_rpc(value: dict[str, Any], user_lamports: int = 5_000_000):
    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            return {"result": {"value": {"lamports": user_lamports}}}
        if method == "simulateTransaction":
            return {"result": {"value": value}}
        raise AssertionError(f"unexpected method {method}")

    return rpc


def test_simulate_landing_bundle_passes_with_ata_prelude() -> None:
    # WITH the ATA prelude the buy lands: the fake RPC returns err:null + units → the
    # two-pass sets the CU limit from measured units (50_000 × 1.2 = 60_000).
    buy_ix = orquestra_instruction_to_solders(CANNED_BUY)
    ata_ix = create_idempotent_ata_ix(USER, USER, MINT, ASSOCIATED_USER, TOKEN_2022)
    value = {"err": None, "unitsConsumed": 50_000, "logs": ["Program success"]}
    receipt, limit = simulate_landing_bundle(
        buy_ix,
        [ata_ix],
        USER,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value),
        track=[USER],
    )
    assert receipt.status == "pass"
    assert receipt.revert_class is None
    assert limit == 60_000


def test_simulate_landing_bundle_without_ata_reverts_3012() -> None:
    # The DIFFERENTIAL guard: drop the ATA prelude and the same buy reverts AnchorError
    # 3012 (associated_user AccountNotInitialized) → classified account_error.
    buy_ix = orquestra_instruction_to_solders(CANNED_BUY)
    value = {
        "err": {"InstructionError": [3, {"Custom": 3012}]},
        "unitsConsumed": 8_000,
        "logs": [
            "Program log: AnchorError caused by account: associated_user. "
            "Error Code: AccountNotInitialized. Error Number: 3012."
        ],
    }
    receipt, _ = simulate_landing_bundle(
        buy_ix, [], USER, rpc_url="http://127.0.0.1:8899", rpc_call=_sim_rpc(value)
    )
    assert receipt.status == "fail"
    assert receipt.revert_class == "account_error"


# --- BOUNDARY: no signing / broadcast path anywhere in the landing layer ------


def _code_identifiers(path: Path) -> set[str]:
    """Every identifier the module's CODE actually uses — names, attribute accesses, and
    imported aliases. Docstrings, comments and string literals are ignored (they legitimately
    say "never sendTransaction"); only executable references count."""
    import ast

    tree = ast.parse(path.read_text())
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.alias):
            identifiers.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                identifiers.add(node.asname)
    return identifiers


def test_no_sign_or_broadcast_path_in_landing_layer() -> None:
    # Structural, non-negotiable: Gecko assembles standard instructions to PROVE a bundle
    # lands ($0, sigVerify:false, replaceRecentBlockhash:true) — it NEVER signs or sends.
    root = Path(__file__).resolve().parent.parent / "gecko"
    sources = [
        root / "landing.py",
        root / "pump_curve.py",
        root / "providers" / "pumpfun_landing.py",
    ]
    forbidden = {
        "sendTransaction",
        "send_transaction",
        "broadcast",
        "Keypair",
        "sign",
        "sign_message",
        "partial_sign",
        "new_signed",
        "secret_key",
        "from_seed",
    }
    for path in sources:
        used = _code_identifiers(path)
        leaked = forbidden & used
        assert not leaked, f"{path.name} must not reference {sorted(leaked)} in code"
    # positive: the only tx the assembler builds is UNSIGNED, fit only for simulateTransaction
    assert "new_unsigned" in _code_identifiers(root / "landing.py")
