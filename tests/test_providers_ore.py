"""ORE V3 (regolith-labs/ore) — config-driven PDA surface + local TDD.

Recipes live in packaged config (gecko/providers/configs/orquestra/ore.json). We prove
they derive the REAL mainnet addresses offline ($0), that `round` is modelled as an
honest resolver, and — the load-bearing case — that the `stake` account is derived under
its CORRECT owning program (the separate `ore-stake` program it's CPI'd into), not ORE.
A naive IDL/llms.txt tool that assumes ORE owns `stake` produces a silently WRONG address;
this test pins both so the fix can't regress.

Ground truth: regolith-labs/ore@master consts + real mainnet accounts (config/treasury/
board all owned by ORE; the stake fixture owned by ore-stake). The real on-chain gate is
env-gated (GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.
"""

from __future__ import annotations

import os

import pytest

from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode, derive_pda
from gecko.pda_testkit import SurfpoolError, SurfpoolFork, verify_derivation
from gecko.provider_config import load_packaged_provider

ORE = "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"
STAKE_PROGRAM = "stakecNP3FpiExZPCgZfqRgumVzi6dNqnfrjwXyTgeH"
SIGNER = "HBUh9g46wk2X89CvaNN15UmsznP59rh6od1h8JwYAopk"

CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"
TREASURY = "45db2FSR4mcXdSVVZbKbwojU6uYDpMyhpEi7cC8nHaWG"
BOARD = "BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi"
STAKE_CORRECT = "6bcZmEtjPSHQbb3dUVFrtTMjFpihD5fPXHdhDtYCyzp"
STAKE_WRONG_UNDER_ORE = "EPupXtqMaQw4QSKTxPo2PFFGkUj47SDPwHwtQhSq9Pg3"


def _ore_pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["ore"].program
    assert program is not None
    return program.pdas


# --- servable: the config now carries an orquestra_project, so it builds a surface ---


def test_ore_is_servable_derivation_only() -> None:
    # Sprint 1 wired the slug → build_surface_from_config succeeds; the surface exposes
    # get_program_graph + derive_pda + the generic simulate tool (no plan intents yet).
    from gecko.providers.cli import PROGRAMS

    _, apis = load_packaged_provider("orquestra")
    program = apis["ore"].program
    assert program is not None
    assert program.orquestra_project == "6alwvs9936laepljczqumb"

    surface = PROGRAMS["ore"]()
    assert surface.program_id == ORE
    assert (
        surface.project_base_url
        == "https://api.orquestra.dev/api/6alwvs9936laepljczqumb"
    )
    tool_names = {t["name"] for t in surface.list_tools()}
    assert tool_names == {"get_program_graph", "derive_pda", "simulate"}
    out = surface.call_tool("derive_pda", {"account": "config", "bindings": {}})
    assert out["address"] == CONFIG


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
