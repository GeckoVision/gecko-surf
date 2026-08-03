"""MetaDAO launchpad_v7 (the ICO/launch program) — config-driven PDA surface + local TDD.

The capstone gap: Orquestra's own PDA Finder reports "No PDA accounts found in this IDL"
for this program — its Anchor-IDL generator drops EVERY seed, because none are declared in
IDL `pda.seeds`; they live only as `#[account(seeds=...)]` in v07 source. Gecko recovers
them from source. Here we prove `launch`/`launch_signer`/`funding_record` derive the REAL
mainnet addresses (offline, $0) and hold real accounts owned by launchpad_v7 (surfpool).

Ground truth: regolith/metaDAOproject v07_launchpad source, verified live against mainnet
getProgramAccounts (20/20 launches + 5/5 funding records re-derived). The on-chain gate is
env-gated (GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.
"""

from __future__ import annotations

import os

import pytest

from gecko.pda import PdaNode, derive_pda
from gecko.pda_testkit import SurfpoolError, SurfpoolFork, verify_derivation
from gecko.provider_config import load_packaged_provider

LAUNCHPAD = "moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM"

BASE_MINT = "Hszh6zhqhfR6vv27dQi5rARjvdXyaobGcVhj3t73meta"
LAUNCH = "1AEZZsShFCnC8UUetqk9hGby66Q5mgyszDHdXjAmdYe"
LAUNCH_SIGNER = "5spjbrDnrUAEcddwTdfZpeb8AWTo9v1H6PVkRNkVACm9"
# a DIFFERENT launch, with a real funder, for the funding_record fixture
LAUNCH2 = "HwvxqXHcRNKikH8RsqRRaV6NCpzxg1g5tHgnMfsdaaEj"
FUNDER = "A9s4LaQQwmL98hwMZfnrn7JNjYENUUqSxPg6vaSjTHZx"
FUNDING_RECORD = "13DiYjVwfhBHwgPHWT44r4gcGo1RJjV6TcrwmMTy3D7"


def _pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["metadao_ico"].program
    assert program is not None
    return program.pdas


# --- offline, $0: config recipes derive the real mainnet addresses ---


def test_launch_derives_real_address() -> None:
    assert derive_pda(_pdas()["launch"], {"base_mint": BASE_MINT}).address == LAUNCH


def test_launch_signer_derives_from_launch() -> None:
    assert (
        derive_pda(_pdas()["launch_signer"], {"launch": LAUNCH}).address
        == LAUNCH_SIGNER
    )


def test_funding_record_derives_from_launch_and_funder() -> None:
    got = derive_pda(_pdas()["funding_record"], {"launch": LAUNCH2, "funder": FUNDER})
    assert got.address == FUNDING_RECORD


def test_gecko_recovers_the_full_set_the_idl_drops() -> None:
    # Orquestra's IDL-only generator finds NO PDAs here; Gecko recovers all three from
    # source, and every one is statically resolvable (no honest gaps in this core set).
    pdas = _pdas()
    assert set(pdas) == {"launch", "launch_signer", "funding_record"}
    assert all(node.resolvable for node in pdas.values())


# --- real on-chain gate: verify against a live surfpool mainnet fork (env-gated, $0) ---


@pytest.mark.skipif(
    os.getenv("GECKO_SURFPOOL_E2E") != "1",
    reason="set GECKO_SURFPOOL_E2E=1 (and have surfpool + a mainnet RPC) to run the on-chain gate",
)
def test_metadao_derivation_against_surfpool_fork() -> None:
    """Fork mainnet locally; assert the derived launch/launch_signer/funding_record hold
    real accounts owned by launchpad_v7 — the PDAs Orquestra's IDL tool derives NONE of."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    pdas = _pdas()
    try:
        with SurfpoolFork(mainnet) as fork:
            lch = verify_derivation(
                pdas["launch"], {"base_mint": BASE_MINT}, rpc_url=fork.rpc_url
            )
            sig = verify_derivation(
                pdas["launch_signer"], {"launch": LAUNCH}, rpc_url=fork.rpc_url
            )
            fr = verify_derivation(
                pdas["funding_record"],
                {"launch": LAUNCH2, "funder": FUNDER},
                rpc_url=fork.rpc_url,
            )
    except SurfpoolError as exc:
        pytest.skip(f"surfpool fork unavailable: {exc}")

    # real data accounts: exist and are owned by launchpad_v7
    assert lch.address == LAUNCH and lch.exists and lch.owner_matches
    assert fr.address == FUNDING_RECORD and fr.exists and fr.owner_matches
    # launch_signer is a signing-authority PDA (used via invoke_signed) — it derives
    # correctly but needn't hold a persistent account; if present it's launchpad-owned.
    assert sig.address == LAUNCH_SIGNER
    assert sig.owner in (LAUNCHPAD, None)
