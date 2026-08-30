"""ORE V3 (regolith-labs/ore) — config-driven PDA surface, the claim plan, local TDD.

Recipes live in packaged config (gecko/providers/configs/orquestra/ore.json). We prove
they derive the REAL mainnet addresses offline ($0), that `round` is modelled as an
honest resolver, and — the load-bearing case — that the `stake` account is derived under
its CORRECT owning program (the separate `ore-stake` program it's CPI'd into), not ORE.
A naive IDL/llms.txt tool that assumes ORE owns `stake` produces a silently WRONG address;
this test pins both so the fix can't regress.

The claim half pins the ORE-specific comprehension gaps, all source-verified against
regolith-labs/ore@master and cross-checked against real mainnet instructions:

  * the 11-account `claimOre` set, including the three accounts the live Orquestra
    instruction surface reports `pda: null` (and no address) for;
  * **authority-vs-signer**: claim_ore seeds miner by the SIGNER *and* asserts
    `miner.authority == signer`, so claiming for another authority is impossible, not
    just differently-derived — plan_claim refuses instead of deriving a rejected miner;
  * the optional `bps` arg the shipped idl.json/Orquestra declare as `args: []`;
  * the Miner layout the shipped idl.json gets wrong by 208 bytes.

Ground truth: regolith-labs/ore@master consts + real mainnet accounts (config/treasury/
board all owned by ORE; the stake fixture owned by ore-stake). The real on-chain gate is
env-gated (GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.
"""

from __future__ import annotations

import base64
import os
import struct
from typing import Any

import pytest

from gecko.ore_state import (
    MINER_ACCOUNT_SIZE,
    decode_miner_state,
    decode_treasury_state,
)
from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode, derive_pda
from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
    verify_derivation,
)
from gecko.provider_config import load_packaged_provider

ORE = "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"
STAKE_PROGRAM = "stakecNP3FpiExZPCgZfqRgumVzi6dNqnfrjwXyTgeH"
SIGNER = "HBUh9g46wk2X89CvaNN15UmsznP59rh6od1h8JwYAopk"

CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"
TREASURY = "45db2FSR4mcXdSVVZbKbwojU6uYDpMyhpEi7cC8nHaWG"
BOARD = "BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi"
STAKE_CORRECT = "6bcZmEtjPSHQbb3dUVFrtTMjFpihD5fPXHdhDtYCyzp"
STAKE_WRONG_UNDER_ORE = "EPupXtqMaQw4QSKTxPo2PFFGkUj47SDPwHwtQhSq9Pg3"

# --- claim fixtures: a REAL mainnet miner and its authority (2026-08-05) -------------
ORE_MINT = "oreoU2P8bN6jkk3jbaiVxYnG1dCXcYxwhwyK9jSybcp"
CLAIM_SIGNER = "5D1oAw6sE14YvdSPwGWGbFCj7SVTbivnTxWXxbvNrur6"
MINER = "3iRVwgvcTJCj6qCK3hcBhyZiXDNzmmBKwcryR6tjJjGw"
RECIPIENT = "7y6t7DhGwvm2pXgBhpwESnDs2w1cyGwVXiVnFGNwdACF"
TREASURY_TOKENS = "GwZS8yBuPPkPgY4uh7eEhHN5EEdpkf7EBZ1za6nuP3wF"
OTHER_AUTHORITY = "EPSPMv1F94t5aF2PB8d1x3MV2tWRSQ5ca43dReEmzgCm"

# The REAL Miner account data (752 bytes, mainnet 2026-08-05). Frozen, so the decoded
# numbers below are stable even as the live account keeps mining.
REAL_MINER_BLOB = (
    "ZwAAAAAAAAA+gp4VpgoH21ITtk+o3TRxmnnUVEBR21p93ZuW8EzGhwEAAAAAAAAABngEAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAA0AcAAAAAAADQBwAAAAAAANAHAAAAAAAAAAAAAAAAAADQBwAAAAAAANAH"
    "AAAAAAAA0AcAAAAAAADQBwAAAAAAANAHAAAAAAAA0AcAAAAAAADQBwAAAAAAANAHAAAAAAAA0AcA"
    "AAAAAAAAAAAAAAAAANAHAAAAAAAA0AcAAAAAAADQBwAAAAAAANAHAAAAAAAA0AcAAAAAAADQBwAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAABCk0AAAAAAAOIZQgAAAAAAQpNAAAAAAABCk0AAAAAAAOIZQgAAAAAAQpNAAAAAAABC"
    "k0AAAAAAAEKTQAAAAAAAQpNAAAAAAADiGUIAAAAAAEKTQAAAAAAAQpNAAAAAAADiGUIAAAAAAEKT"
    "QAAAAAAA4hlCAAAAAADiGUIAAAAAAOIZQgAAAAAAQpNAAAAAAABCk0AAAAAAAEKTQAAAAAAA4hlC"
    "AAAAAABCk0AAAAAAAEKTQAAAAAAA4hlCAAAAAADiGUIAAAAAAAZ4BAAAAAAAnCqSzmW0AAAAAAAA"
    "AAAAAAegAAAAAAAAVJVD5uRmAABzNISNFJsAAAAAAAAAAAAAp80oagAAAACBFGRj9vsBAIAn2YsG"
    "AQAAvqs/Q3cBAAA="
)
# The REAL Treasury singleton (48 bytes, same snapshot).
REAL_TREASURY_BLOB = "aAAAAAAAAAAAMOiATwMAAKq9mUZiyAAAAAAAAAAAAADMU385/vsIACKRH3c1Rx8A"


def _ore_pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["ore"].program
    assert program is not None
    return program.pdas


# --- servable: the config now carries an orquestra_project, so it builds a surface ---


def test_ore_is_servable_with_the_claim_intent() -> None:
    # build_surface_from_config succeeds; the surface exposes the graph/derive/simulate
    # tools PLUS the plan_claim intent wired this sprint.
    from gecko.providers.cli import PROGRAMS

    _, apis = load_packaged_provider("orquestra")
    program = apis["ore"].program
    assert program is not None
    assert program.orquestra_project == "6alwvs9936laepljczqumb"
    assert tuple(program.intents) == ("plan_claim",)

    surface = PROGRAMS["ore"]()
    assert surface.program_id == ORE
    assert (
        surface.project_base_url
        == "https://api.orquestra.dev/api/6alwvs9936laepljczqumb"
    )
    tool_names = {t["name"] for t in surface.list_tools()}
    assert tool_names == {
        "get_program_graph",
        "derive_pda",
        "simulate",
        "plan_claim",
    }
    out = surface.call_tool("derive_pda", {"account": "config", "bindings": {}})
    assert out["address"] == CONFIG
    # the decimals honesty reaches the agent-facing tool description (ORE has 11, not 9)
    claim_tool = next(t for t in surface.list_tools() if t["name"] == "plan_claim")
    assert "ELEVEN decimals" in claim_tool["description"]


# --- offline, $0: config recipes derive the real mainnet addresses ---


def test_singletons_derive_real_addresses() -> None:
    pdas = _ore_pdas()
    assert derive_pda(pdas["config"], {}).address == CONFIG
    assert derive_pda(pdas["treasury"], {}).address == TREASURY
    assert derive_pda(pdas["board"], {}).address == BOARD


def test_round_is_an_honest_resolver() -> None:
    # round's id is read from board/miner account data — not a free-choice argument.
    node = _ore_pdas()["round"]
    assert node.resolvable is False
    assert node.unresolved_seeds[0].depends_on == ("board",)


def test_stake_is_derived_under_its_correct_cross_program_owner() -> None:
    # `stake` is CPI'd across a program boundary — it belongs to ore-stake, NOT ORE.
    # Our config pins the correct owning program, so it derives the real address.
    node = _ore_pdas()["stake"]
    assert node.program_id == STAKE_PROGRAM
    assert derive_pda(node, {"signer": SIGNER}).address == STAKE_CORRECT


def test_stake_under_ore_program_is_the_naive_tool_bug() -> None:
    # THE GAP, pinned: derive the SAME seeds under the ORE program (what a naive
    # IDL/llms.txt tool assumes) and you get a different, WRONG address. Gecko getting
    # the owning program right is the whole correctness fix — regressing it must fail here.
    wrong = PdaNode(
        "stake",
        (
            ConstantPdaSeedNode(b"stake", encoding="utf8"),
            VariablePdaSeedNode("signer", source="account", encoding="pubkey"),
        ),
        program_id=ORE,
    )
    got = derive_pda(wrong, {"signer": SIGNER}).address
    assert got == STAKE_WRONG_UNDER_ORE
    assert got != STAKE_CORRECT  # the naive owner gives a silently wrong account


# --- the Miner/Treasury decoders (the reads behind plan_claim) ----------------------


def test_decode_miner_against_the_real_mainnet_blob() -> None:
    """The decoder walks REAL on-chain bytes. THE finding: the account is 752 bytes
    (8-byte Steel discriminator + the 744-byte deployed struct), while the IDL ORE
    ships (api/idl.json, metadata.origin "steel") describes a 536-byte Miner — it drops
    `auto_return` and `mass: [u64; 25]` and reorders the rest. An IDL-driven decoder
    reads `rewards_ore` 208 bytes early and reports garbage; ours reads the real value
    and the authority that derives back to this very account."""
    raw = base64.b64decode(REAL_MINER_BLOB)
    assert len(raw) == MINER_ACCOUNT_SIZE == 752
    state = decode_miner_state(raw)
    assert state.authority == CLAIM_SIGNER
    # the authority round-trips through the source seed recipe → THIS account
    assert derive_pda(_ore_pdas()["miner"], {"authority": state.authority}).address == (
        MINER
    )
    assert state.refined_ore == 113_133_301_765_460
    assert state.rewards_ore == 170_512_575_902_835
    assert state.claimable_ore == 283_645_877_668_295  # ≈ 2836.45 ORE at 11 decimals
    assert state.checkpoint_pending is False  # checkpoint_id == round_id == 292870

    # the IDL layout would place rewards_ore at struct offset 488, not 696 — a
    # different (wrong) number. Pin the divergence so "just use the IDL" can't return.
    idl_offset_value = struct.unpack_from("<Q", raw, 8 + 488)[0]
    assert idl_offset_value != state.rewards_ore


def test_decode_rejects_a_foreign_account() -> None:
    from gecko.ore_state import OreStateError

    with pytest.raises(OreStateError, match="discriminator"):
        decode_miner_state(b"\x01" * 752)


def test_claim_preview_mirrors_the_source_fee_math() -> None:
    """`claim_ore` charges 10% on the UNREFINED portion only (rewards_ore), never on
    refined_ore — get that backwards and the payout preview is wrong by ~11%."""
    miner = decode_miner_state(base64.b64decode(REAL_MINER_BLOB))
    treasury = decode_treasury_state(base64.b64decode(REAL_TREASURY_BLOB))

    amount, fee = miner.claim_preview(treasury, 10_000)
    assert fee == miner.rewards_ore // 10
    assert amount == miner.refined_ore + miner.rewards_ore - fee
    # a partial claim scales BOTH balances and the fee — linearly, floor-divided
    half_amount, half_fee = miner.claim_preview(treasury, 5_000)
    assert half_fee == (miner.rewards_ore // 2) // 10
    assert half_amount == (miner.refined_ore // 2) + (miner.rewards_ore // 2) - half_fee


# --- plan_claim: the 11-account set + the ORE-specific traps ------------------------


def _claim_rpc(
    miner_blob: str = REAL_MINER_BLOB, treasury_blob: str = REAL_TREASURY_BLOB
):
    """An injected transport serving the two control-plane reads plan_claim makes.

    Any non-treasury address gets the same (real) miner blob — so a plan for the WRONG
    signer still finds an account, and the authority assertion (not a missing account)
    is what refuses it. That is the gate we want under test."""

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            blob = treasury_blob if params[0] == TREASURY else miner_blob
            return {"result": {"value": {"owner": ORE, "data": [blob, "base64"]}}}
        raise AssertionError(f"unexpected RPC {method} {params[:1]}")

    return rpc


def test_plan_claim_derives_the_full_eleven_account_set() -> None:
    from gecko.providers.ore import plan_claim

    plan = plan_claim({"signer": CLAIM_SIGNER}, rpc_call=_claim_rpc())
    assert plan["instruction"] == "claimOre"
    # the project's camelCase wire names, in the source's account order
    assert list(plan["accounts"]) == [
        "signer",
        "board",
        "miner",
        "mint",
        "recipient",
        "treasury",
        "treasuryTokens",
        "systemProgram",
        "tokenProgram",
        "associatedTokenProgram",
        "oreProgram",
    ]
    accounts = plan["accounts"]
    assert accounts["miner"] == MINER
    assert accounts["board"] == BOARD
    assert accounts["treasury"] == TREASURY
    # THE three the live instruction surface reports `pda: null` and no address for
    assert accounts["mint"] == ORE_MINT
    assert accounts["recipient"] == RECIPIENT
    assert accounts["treasuryTokens"] == TREASURY_TOKENS


def test_plan_claim_refuses_a_signer_that_is_not_the_miner_authority() -> None:
    """THE Class-1 regression. Orquestra's config note said `miner` binds an authority
    that is NOT the tx signer — true for deploy/checkpoint, FALSE for claimOre:
    claim_ore both seeds miner by the signer AND asserts miner.authority == signer.
    So a claim for another authority is impossible, not merely differently-derived —
    plan_claim must refuse rather than hand back a miner the program will reject."""
    from gecko.providers.ore import OrePlanError, plan_claim

    with pytest.raises(OrePlanError, match="another authority"):
        plan_claim(
            {"signer": CLAIM_SIGNER, "authority": OTHER_AUTHORITY},
            rpc_call=_claim_rpc(),
        )
    # and the state read is a second, independent gate: a signer whose derived miner
    # decodes to a DIFFERENT authority is refused too (never silently planned).
    with pytest.raises(OrePlanError, match="different authority"):
        plan_claim({"signer": OTHER_AUTHORITY}, rpc_call=_claim_rpc())


def test_plan_claim_declares_the_source_true_bps_bytes() -> None:
    """The dropped-arg gap, pinned. bps=10000 is the canonical omit form ("04") that
    the program reads as 100%; anything less appends the u64 LE bps — bytes the
    builder does not emit (it returns "04" regardless, wire-verified)."""
    from gecko.providers.ore import claim_instruction_data, plan_claim

    full = plan_claim({"signer": CLAIM_SIGNER}, rpc_call=_claim_rpc())
    assert full["requested_bps"] == 10_000
    assert full["instruction_data"] == "04"
    assert full["args"] == {}  # what /build can actually carry: nothing

    half = plan_claim({"signer": CLAIM_SIGNER, "bps": 5000}, rpc_call=_claim_rpc())
    assert half["instruction_data"] == "04" + struct.pack("<Q", 5000).hex()
    assert claim_instruction_data(10_000) == "04"
    assert "bps" in half["gaps"]


def test_plan_claim_reads_the_claimable_balance_and_checkpoint_verdict() -> None:
    from gecko.providers.ore import plan_claim

    state = plan_claim({"signer": CLAIM_SIGNER}, rpc_call=_claim_rpc())["miner_state"]
    assert state["decimals"] == 11  # ORE has ELEVEN decimals, not 9
    assert state["claimable_ore"] == 283_645_877_668_295
    assert state["claim_amount_at_least"] == 266_594_620_078_012
    assert state["refining_fee"] == 17_051_257_590_283
    assert state["checkpoint_pending"] is False


def test_plan_claim_landing_plan_declares_no_ata_prelude() -> None:
    """Source-verified: claim_ore calls create_associated_token_account ITSELF when
    `recipient` is empty (payer = signer), so declaring an idempotent-ATA prelude here
    would be inventing work — the same class as MetaDAO's init_if_needed."""
    from gecko.landing import ASSOCIATED_TOKEN_PROGRAM_ID
    from gecko.providers.ore import plan_claim

    plan = plan_claim({"signer": CLAIM_SIGNER}, rpc_call=_claim_rpc())
    kinds = [step["kind"] for step in plan["landing_plan"]]
    assert kinds == ["compute_budget", "compute_budget", "claimOre"]
    assert ASSOCIATED_TOKEN_PROGRAM_ID not in {
        step["program"] for step in plan["landing_plan"]
    }
    assert "NO prelude is needed" in plan["preconditions"]["recipient"]


def test_plan_claim_never_touches_the_flagged_round_pda() -> None:
    """Trap check: `round`'s seed is runtime account data (board.round_id /
    miner.round_id) — an honest FLAGGED resolver. claimOre does not take it, so the
    claim path never needs to resolve it (checkpoint does — declared, not wired)."""
    from gecko.providers.ore import ORE_STARTS, plan_claim

    plan = plan_claim({"signer": CLAIM_SIGNER}, rpc_call=_claim_rpc())
    assert "round" not in {a.lower() for a in plan["accounts"]}
    assert "round" not in ORE_STARTS["plan_claim"].accounts
    assert any(gap.name == "checkpoint" for gap in ORE_STARTS["plan_claim"].gaps)


def test_find_start_routes_a_claim_intent_to_plan_claim() -> None:
    from gecko.find_start import find_start

    result = find_start("claim my ore mining rewards")
    assert not result.no_start
    top = result.starts[0]
    assert (top.program, top.instruction, top.next_tool) == (
        "ore",
        "claimOre",
        "plan_claim",
    )
    # the honest gaps ride along with the start point, never dropped
    assert {gap.name for gap in top.gaps} >= {"bps", "checkpoint"}


# --- real on-chain gate: verify against a live surfpool mainnet fork (env-gated, $0) ---


@pytest.mark.skipif(
    os.getenv("GECKO_SURFPOOL_E2E") != "1",
    reason="set GECKO_SURFPOOL_E2E=1 (and have surfpool + a mainnet RPC) to run the on-chain gate",
)
def test_ore_derivation_against_surfpool_fork() -> None:
    """Fork mainnet locally; assert ORE singletons are owned by ORE and `stake` is owned
    by ore-stake — the cross-program ownership our config gets right, at $0, no signing."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    pdas = _ore_pdas()
    try:
        with SurfpoolFork(mainnet) as fork:
            cfg = verify_derivation(pdas["config"], {}, rpc_url=fork.rpc_url)
            tre = verify_derivation(pdas["treasury"], {}, rpc_url=fork.rpc_url)
            brd = verify_derivation(pdas["board"], {}, rpc_url=fork.rpc_url)
            stk = verify_derivation(
                pdas["stake"], {"signer": SIGNER}, rpc_url=fork.rpc_url
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

    # singletons always exist and are owned by ORE
    assert cfg.address == CONFIG and cfg.exists and cfg.owner_matches
    assert tre.address == TREASURY and tre.exists and tre.owner_matches
    assert brd.address == BOARD and brd.exists and brd.owner_matches
    # the cross-program account: derived correctly; a per-user stake may be closed
    # (withdrawn), so existence isn't guaranteed — but if present it's ore-stake-owned,
    # NEVER ORE. That correct owner is the whole point (proven offline above too).
    assert stk.address == STAKE_CORRECT
    assert stk.owner in (STAKE_PROGRAM, None)
