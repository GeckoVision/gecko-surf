"""MetaDAO launchpad_v7 (the ICO/launch program) — config-driven PDA surface + local TDD.

The capstone gap: Orquestra's own PDA Finder reports "No PDA accounts found in this IDL"
for this program — its Anchor-IDL generator drops EVERY seed, because none are declared in
IDL `pda.seeds`; they live only as `#[account(seeds=...)]` in v07 source. Gecko recovers
them from source. Here we prove `launch`/`launch_signer`/`funding_record` derive the REAL
mainnet addresses (offline, $0) and hold real accounts owned by launchpad_v7 (surfpool),
and that `plan_fund` assembles the full 10-account fund set — including the has_one-read
USDC vault and the fund-window verdict — against a REAL mainnet Launch blob.

Ground truth: regolith/metaDAOproject v07_launchpad source, verified live against mainnet
getProgramAccounts (20/20 launches + 5/5 funding records re-derived); fund account
list/args cross-checked against the live Orquestra instruction surface AND
v07 instructions/fund.rs. The on-chain gate is env-gated (GECKO_SURFPOOL_E2E=1) so the
default suite stays offline and $0.
"""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import pytest

from gecko.landing import SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
from gecko.metadao_state import (
    LaunchAccountState,
    MetadaoStateError,
    decode_launch_state,
)
from gecko.pda import PdaNode, derive_pda
from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
    verify_derivation,
)
from gecko.provider_config import load_packaged_provider
from gecko.providers.metadao import plan_fund

LAUNCHPAD = "moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM"

BASE_MINT = "Hszh6zhqhfR6vv27dQi5rARjvdXyaobGcVhj3t73meta"
LAUNCH = "1AEZZsShFCnC8UUetqk9hGby66Q5mgyszDHdXjAmdYe"
LAUNCH_SIGNER = "5spjbrDnrUAEcddwTdfZpeb8AWTo9v1H6PVkRNkVACm9"
# a DIFFERENT launch, with a real funder, for the funding_record + fund fixtures
LAUNCH2 = "HwvxqXHcRNKikH8RsqRRaV6NCpzxg1g5tHgnMfsdaaEj"
BASE_MINT2 = "FgVcn25UXf4MoNf4coaz2o5xDGPK1goMbUBfyUgrmeta"
FUNDER = "A9s4LaQQwmL98hwMZfnrn7JNjYENUUqSxPg6vaSjTHZx"
FUNDING_RECORD = "13DiYjVwfhBHwgPHWT44r4gcGo1RJjV6TcrwmMTy3D7"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# LAUNCH2's real on-chain state (decoded from the embedded blob below; RPC-verified)
LAUNCH2_QUOTE_VAULT = "7zCkhwwWvSfZXX6bt1NzjoM5MYYf3nwJNHZvraLnmtai"
# the funder's ASSOCIATED USDC token account (exists on mainnet) + the #[event_cpi] PDA
FUNDER_USDC_ATA = "EzqUajAV927r4i8nvcayYxjoJkAEuJ9zJgBudDgFhBT2"
EVENT_AUTHORITY = "Cv3ApcGZMYzAzKF3vuAbWzwbLvicw17ePYMvrSedGLE4"

# The REAL LAUNCH2 account data (785 bytes, mainnet 2026-08-05) — the decoder is
# tested against genuine on-chain bytes, not an encoder that could share its bugs.
REAL_LAUNCH2_BLOB = (
    "kDMzo85V1Sb8AFyy7CIAAAAA1hF+AwAAAAQAAAA6GgXT5gdYELKHjIXdViUNMKe81R6wNVgs1Tr+"
    "TgLX7OAng2fbumFDFiqocoEkxX0rNvIUoTqmrNSvacvaO3aBSKrSzEpkAuUM/3eWwvi5PFKX1bSx"
    "4jtYKMy265ineK0SrZwwEuBndgpa3kQD9qco3EKjMgymbONP53HjWEhUBAT6Iun9SsGOvlxxX4cg"
    "VLVLCdsuI/XYLVrfXtzxPHTPR25H/MM7uLiKYreK438x7SDSrqYWn81dBuxoDI6Hoq/+Z82EeCgB"
    "ZUje7/scM1u4HmX6EiR7/aPLMq1uxmJ1yR+qjrLLPGXCqE0mM5PDVUREAL63o4SQLrrdvU6K12oP"
    "nNohoCxWJD9xXATKErFB1DAt37mS66Vk3RB3hGM7dpbzxvp6877brTo9ZfNqq8l0MbG75MLS9uDk"
    "fKYCA0UvXWEBj4BnagAAAAABnMZsagAAAADAAMQycggAAAMWBgAAAAAAAABGBQABbWJ6ivXrn7LD"
    "Ms3iQlCVcyDdUH3g0tS/JBvB9WH+4K8BTSQ8MgUa0JmKJSFtggw6TWeZfOrFTd/FFm+AAOjFKuiw"
    "3rM6qv832T1Gia8OGZP60wwSPqBVGTBJm+pSa8EjbQDo6oO7CwAAErDeszqq/zfZPUaJrw4Zk/rT"
    "DBI+oFUZMEmb6lJrwSNtAFyy7CIAAAAAAAAAAAAAAAAAAUPHbGoAAAAAAQAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


def _live_launch_blob() -> str:
    """A synthetic LIVE Launch blob with LAUNCH2's real field values but
    state=Live and a far-open window — encodes the same Borsh layout
    decode_launch_state walks (the REAL-blob test below guards against the
    encoder and decoder sharing a wrong assumption)."""
    from solders.pubkey import Pubkey

    def pk(addr: str) -> bytes:
        return bytes(Pubkey.from_string(addr))

    raw = bytearray()
    raw += bytes.fromhex("903333a3ce55d526")  # sha256("account:Launch")[:8]
    raw += bytes([255])  # pda_bump
    raw += struct.pack("<Q", 150_000_000_000)  # minimum_raise_amount
    raw += struct.pack("<Q", 25_000_000_000)  # monthly_spending_limit_amount
    raw += struct.pack("<I", 1) + pk(FUNDER)  # members Vec<Pubkey> (len 1)
    raw += pk(FUNDER)  # launch_authority (any valid pubkey)
    raw += pk("5oqUq2YxLr14kbZaubhkog641r1UGRq43sy4G58iQbka")  # launch_signer
    raw += bytes([254])  # launch_signer_pda_bump
    raw += pk(LAUNCH2_QUOTE_VAULT)
    raw += pk(FUNDER)  # launch_base_vault (unused by fund)
    raw += pk(BASE_MINT2)
    raw += pk(USDC)  # quote_mint
    raw += bytes([1]) + struct.pack("<q", 1_754_300_000)  # started: Some(...)
    raw += bytes([0])  # closed: None
    raw += struct.pack("<Q", 9_286_571_000_000)  # total_committed_amount
    raw += bytes([1])  # state = Live
    raw += struct.pack("<Q", 1558)  # seq_num
    raw += struct.pack("<I", 4_000_000_000)  # seconds_for_launch (window ~2160s AD)
    return base64.b64encode(bytes(raw)).decode()


def _fund_rpc() -> Any:
    blob = _live_launch_blob()

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo" and params[0] == LAUNCH2:
            return {"result": {"value": {"owner": LAUNCHPAD, "data": [blob, "base64"]}}}
        raise AssertionError(f"unexpected RPC {method} {params[:1]}")

    return rpc


def _fund_bindings() -> dict[str, Any]:
    return {"base_mint": BASE_MINT2, "funder": FUNDER, "amount": 10_000_000}


def _pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["metadao_ico"].program
    assert program is not None
    return program.pdas


# --- servable: the config now carries an orquestra_project, so it builds a surface ---


def test_metadao_is_servable_with_the_fund_intent() -> None:
    # build_surface_from_config succeeds; the surface exposes the graph/derive/simulate
    # tools PLUS the plan_fund intent wired this sprint.
    from gecko.providers.cli import PROGRAMS

    _, apis = load_packaged_provider("orquestra")
    program = apis["metadao_ico"].program
    assert program is not None
    assert program.orquestra_project == "krhmrxpy2fgwn3q0whic7"
    assert tuple(program.intents) == ("plan_fund",)

    surface = PROGRAMS["metadao_ico"]()
    assert surface.program_id == LAUNCHPAD
    assert (
        surface.project_base_url
        == "https://api.orquestra.dev/api/krhmrxpy2fgwn3q0whic7"
    )
    tool_names = {t["name"] for t in surface.list_tools()}
    assert tool_names == {"get_program_graph", "derive_pda", "simulate", "plan_fund"}
    out = surface.call_tool(
        "derive_pda", {"account": "launch", "bindings": {"base_mint": BASE_MINT}}
    )
    assert out["address"] == LAUNCH
    # the amount-unit honesty reaches the agent-facing tool description
    fund_tool = next(t for t in surface.list_tools() if t["name"] == "plan_fund")
    assert "6 decimals" in fund_tool["description"]
    assert "10_000_000" in fund_tool["description"]


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
    # Orquestra's IDL-only generator finds NO PDAs here; Gecko recovers the three core
    # recipes from source (+ the fund intent's event_authority/ATA overlay recipes),
    # and every one is statically resolvable (no honest gaps in this set).
    pdas = _pdas()
    assert set(pdas) == {
        "launch",
        "launch_signer",
        "funding_record",
        "event_authority",
        "funder_quote_account",
    }
    assert all(node.resolvable for node in pdas.values())


# --- the Launch state read (the ONE control-plane read behind plan_fund) ---


def test_decode_launch_state_against_the_real_mainnet_blob() -> None:
    """The decoder walks REAL on-chain bytes: the dynamic Vec<Pubkey> prefix means
    no fixed offsets — every decoded field below was independently RPC-verified."""
    state = decode_launch_state(base64.b64decode(REAL_LAUNCH2_BLOB))
    assert isinstance(state, LaunchAccountState)
    assert state.quote_mint == USDC
    assert state.launch_quote_vault == LAUNCH2_QUOTE_VAULT
    assert state.base_mint == BASE_MINT2
    assert state.launch_signer == "5oqUq2YxLr14kbZaubhkog641r1UGRq43sy4G58iQbka"
    assert state.minimum_raise_amount == 150_000_000_000
    assert state.total_committed_amount == 9_286_571_000_000
    assert state.state_name == "Complete"
    assert state.unix_timestamp_started == 1_785_168_015
    assert state.seconds_for_launch == 345_600
    # a Complete launch is honestly declared un-fundable (fund requires Live)
    window_open, reason = state.fund_window(1_785_200_000)
    assert not window_open
    assert "Complete" in reason


def test_decode_launch_state_rejects_a_foreign_discriminator() -> None:
    with pytest.raises(MetadaoStateError, match="discriminator"):
        decode_launch_state(b"\x00" * 200)


def test_decode_launch_state_rejects_truncated_data() -> None:
    real = base64.b64decode(REAL_LAUNCH2_BLOB)
    with pytest.raises(MetadaoStateError, match="truncated"):
        decode_launch_state(real[:80])


def test_fund_window_open_on_a_live_launch() -> None:
    state = decode_launch_state(base64.b64decode(_live_launch_blob()))
    assert state.state_name == "Live"
    window_open, reason = state.fund_window(1_754_300_100)
    assert window_open
    assert "window open" in reason
    # …and expiry is declared with the source's own error name
    expired_open, expired_reason = state.fund_window(1_754_300_000 + 4_000_000_001)
    assert not expired_open
    assert "LaunchExpired" in expired_reason


# --- plan_fund: the full 10-account set, offline, $0 ---


def test_plan_fund_assembles_the_verified_account_set() -> None:
    """Pinned against the live Orquestra instruction surface AND v07 fund.rs:
    the 10 accounts, the project's camelCase wire names, the has_one-read vault."""
    plan = plan_fund(_fund_bindings(), rpc_call=_fund_rpc())
    assert plan["instruction"] == "fund"
    assert plan["build_url"] == (
        "https://api.orquestra.dev/api/krhmrxpy2fgwn3q0whic7/instructions/fund/build"
    )
    assert plan["accounts"] == {
        "launch": LAUNCH2,
        "fundingRecord": FUNDING_RECORD,
        "launchQuoteVault": LAUNCH2_QUOTE_VAULT,  # READ from the launch (has_one)
        "funder": FUNDER,
        "payer": FUNDER,  # defaults to the funder
        "funderQuoteAccount": FUNDER_USDC_ATA,
        "tokenProgram": TOKEN_PROGRAM_ID,  # classic SPL Token, source-pinned
        "systemProgram": SYSTEM_PROGRAM_ID,
        "eventAuthority": EVENT_AUTHORITY,  # #[event_cpi] macro PDA
        "program": LAUNCHPAD,
    }
    # amount is a plain u64 passthrough (no defined-type wrapper on this program)
    assert plan["args"] == {"amount": 10_000_000}
    assert "6 decimals" in plan["args_note"]
    assert plan["feePayer"] == FUNDER


def test_plan_fund_declares_the_fund_window_from_state() -> None:
    plan = plan_fund(_fund_bindings(), rpc_call=_fund_rpc())
    state = plan["launch_state"]
    assert state["state"] == "Live"
    assert state["fund_window_open"] is True
    assert state["quote_mint"] == USDC
    # funding_record init is the PROGRAM's job — declared, never a precondition
    assert "funderQuoteAccount" in plan["preconditions"]
    assert "fundingRecord" not in plan["preconditions"]
    assert "InsufficientFunds" in plan["preconditions"]["funderQuoteAccount"]


def test_plan_fund_ordered_landing_plan() -> None:
    plan = plan_fund(_fund_bindings(), rpc_call=_fund_rpc())
    kinds = [step["kind"] for step in plan["landing_plan"]]
    assert kinds == [
        "compute_budget",
        "compute_budget",
        "create_idempotent_ata",
        "fund",
    ]
    ata = next(s for s in plan["landing_plan"] if s["kind"] == "create_idempotent_ata")
    assert ata["accounts"]["ata"] == FUNDER_USDC_ATA
    assert ata["accounts"]["mint"] == USDC
    assert ata["accounts"]["token_program"] == TOKEN_PROGRAM_ID
    fund = next(s for s in plan["landing_plan"] if s["kind"] == "fund")
    assert fund["accounts"] == plan["accounts"]
    # no remaining accounts on this instruction — nothing declared, nothing hidden
    assert "remaining_accounts" not in fund


def test_plan_fund_payer_can_differ_from_funder() -> None:
    other = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
    plan = plan_fund({**_fund_bindings(), "payer": other}, rpc_call=_fund_rpc())
    assert plan["accounts"]["funder"] == FUNDER
    assert plan["accounts"]["payer"] == other
    assert plan["feePayer"] == other
    # the funding_record stays keyed on the FUNDER, not the payer
    assert plan["accounts"]["fundingRecord"] == FUNDING_RECORD


def test_plan_fund_missing_bindings_raise() -> None:
    with pytest.raises(ValueError, match="amount"):
        plan_fund({"base_mint": BASE_MINT2, "funder": FUNDER}, rpc_call=_fund_rpc())


def test_plan_fund_wrong_base_mint_is_an_honest_error() -> None:
    # A bad base_mint derives a launch address with no account — declared, not built.
    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        return {"result": {"value": None}}

    with pytest.raises(MetadaoStateError, match="not found"):
        plan_fund({**_fund_bindings(), "base_mint": BASE_MINT}, rpc_call=rpc)


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
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"surfpool fork unavailable: {exc}")

    # real data accounts: exist and are owned by launchpad_v7
    assert lch.address == LAUNCH and lch.exists and lch.owner_matches
    assert fr.address == FUNDING_RECORD and fr.exists and fr.owner_matches
    # launch_signer is a signing-authority PDA (used via invoke_signed) — it derives
    # correctly but needn't hold a persistent account; if present it's launchpad-owned.
    assert sig.address == LAUNCH_SIGNER
    assert sig.owner in (LAUNCHPAD, None)
