"""Pump.fun bonding-curve program — config-driven PDA surface + local TDD.

The recipes live in packaged config (gecko/providers/configs/orquestra/pumpfun.json);
here we prove they derive the REAL mainnet addresses (offline, $0, from an injected/
pure derivation) and that the `creator_vault` gap is modelled HONESTLY (a resolver seed,
non-resolvable statically) yet derives correctly once its value is supplied.

Ground truth (RPC-verified from real mainnet Buy tx
55ERcxeiFGo3ix4YLxhrMh99svY5EdvxujL78fEm3xzgn8EArJZmQCwrG3uKmGYXdLWpxAAp8hEJM2Hdz7n3yVK6):
  mint          8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump
  bonding_curve EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH
  creator       Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq  (= bonding_curve.data.creator)
  creator_vault 9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd

The real on-chain gate against a live surfpool mainnet fork is env-gated
(GECKO_SURFPOOL_E2E=1) so the default suite stays offline and $0.
"""

from __future__ import annotations

import os

import pytest

from gecko.pda import ConstantPdaSeedNode, PdaNode, VariablePdaSeedNode, derive_pda
from gecko.pda_testkit import SurfpoolError, SurfpoolFork, verify_derivation
from gecko.provider_config import load_packaged_provider, node_from_spec

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MINT = "8zN8yA21ZGyKRWoxeYqyb2XquHPjVa31Bpxj1bC5pump"
CREATOR = "Cgjdu87kEeTuUGbKh5mAmFnVSeLN189LDbSvNb24J7Mq"
BONDING_CURVE = "EExN5XXyaaE3G3w93WdKbJgMUAH3sFgLJBsg5crNp3tH"
CREATOR_VAULT = "9B1eLfPtyqyTepP98VPosL7s2cQWN29SKMhk2iNTVkqd"
GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
EVENT_AUTHORITY = "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"

# Sprint 2: the full account set — metadata (Metaplex Token Metadata PDA) and the
# associated_bonding_curve (the bonding curve's SPL Associated Token Account).
METADATA = "GDheBHm1UbTQLxweQUNW6aeqZc3uyFx7kHULKonVASza"
ASSOCIATED_BONDING_CURVE = "4cpMS2VREvNkrwgLnoGxAMp6vMZPybzHkseAyzqij3Gi"
METAPLEX_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# This particular fixture mint turns out to be a TOKEN-2022 mint (its owner is the
# Token-2022 program), verified on a surfpool mainnet fork below. So on-chain: the
# classic-Token ATA (4cpMS2…) is a phantom — the REAL associated bonding curve is the
# Token-2022 ATA, and the mint carries its metadata as an in-mint Token-2022 extension
# (metadataPointer + tokenMetadata) rather than a separate Metaplex PDA.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
ASSOCIATED_BONDING_CURVE_T22 = "6qg9ZgTnbeqdmzkuVT6Ffv95nmD2yX6KUxEyVRc1DmDH"


def _pumpfun_pdas() -> dict[str, PdaNode]:
    _, apis = load_packaged_provider("orquestra")
    program = apis["pumpfun"].program
    assert program is not None
    return program.pdas


# --- servable: the config now carries an orquestra_project, so it builds a surface ---


def test_pumpfun_is_servable_derivation_only() -> None:
    # Sprint 1 wired the slug → build_surface_from_config succeeds; the surface exposes
    # get_program_graph + derive_pda (no plan intents yet — those land in a later sprint).
    from gecko.providers.cli import PROGRAMS

    _, apis = load_packaged_provider("orquestra")
    program = apis["pumpfun"].program
    assert program is not None
    assert program.orquestra_project == "6i6q26bmm46b89xlxo1kv"

    surface = PROGRAMS["pumpfun"]()
    assert surface.program_id == PUMP
    assert (
        surface.project_base_url
        == "https://api.orquestra.dev/api/6i6q26bmm46b89xlxo1kv"
    )
    tool_names = {t["name"] for t in surface.list_tools()}
    assert tool_names == {"get_program_graph", "derive_pda"}
    out = surface.call_tool(
        "derive_pda", {"account": "bonding_curve", "bindings": {"mint": MINT}}
    )
    assert out["address"] == BONDING_CURVE


# --- offline, $0: config recipes derive the real mainnet addresses ---


def test_bonding_curve_derives_real_pool() -> None:
    pdas = _pumpfun_pdas()
    got = derive_pda(pdas["bonding_curve"], {"mint": MINT})
    assert got.address == BONDING_CURVE  # the per-mint bonding curve


def test_global_and_event_authority_are_constants() -> None:
    pdas = _pumpfun_pdas()
    assert derive_pda(pdas["global"], {}).address == GLOBAL
    assert derive_pda(pdas["event_authority"], {}).address == EVENT_AUTHORITY


def test_metadata_derives_real_metaplex_pda() -> None:
    # constant-pubkey seed (the Metaplex program id) + the mint → the token's metadata PDA.
    pdas = _pumpfun_pdas()
    got = derive_pda(pdas["metadata"], {"mint": MINT})
    assert got.address == METADATA


def test_associated_bonding_curve_derives_real_ata() -> None:
    # SPL ATA recipe: owner (the bonding curve) + the SPL Token program (constant pubkey)
    # + the mint → the associated token account holding the curve's token reserve.
    pdas = _pumpfun_pdas()
    got = derive_pda(
        pdas["associated_bonding_curve"], {"owner": BONDING_CURVE, "mint": MINT}
    )
    assert got.address == ASSOCIATED_BONDING_CURVE


def test_creator_vault_is_an_honest_gap() -> None:
    # its seed is bonding_curve.data.creator — a field inside another account's data,
    # which we cannot statically resolve. Modelled as a resolver seed → NOT resolvable,
    # dependency declared, never fabricated.
    node = _pumpfun_pdas()["creator_vault"]
    assert node.resolvable is False
    assert node.unresolved_seeds[0].depends_on == ("bonding_curve",)


def test_creator_vault_derives_once_the_gap_is_resolved() -> None:
    # THE VALUE: naive IDL->derivation drops the dotted-path seed; Gecko knows the full
    # recipe, so given the resolved creator the vault derives first-plan-correct.
    resolved = PdaNode(
        "creator_vault",
        (
            ConstantPdaSeedNode(b"creator-vault", encoding="utf8"),
            VariablePdaSeedNode("creator", source="account", encoding="pubkey"),
        ),
        program_id=PUMP,
    )
    assert derive_pda(resolved, {"creator": CREATOR}).address == CREATOR_VAULT


# --- real on-chain gate: verify against a live surfpool mainnet fork (env-gated, $0) ---


@pytest.mark.skipif(
    os.getenv("GECKO_SURFPOOL_E2E") != "1",
    reason="set GECKO_SURFPOOL_E2E=1 (and have surfpool + a mainnet RPC) to run the on-chain gate",
)
def test_pumpfun_derivation_against_surfpool_fork() -> None:
    """Fork mainnet locally; assert the derived PDAs hold real accounts owned by Pump.
    The strongest correctness proof, at $0 — never touches mainnet, never signs."""
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    pdas = _pumpfun_pdas()
    resolved_vault = PdaNode(
        "creator_vault",
        (
            ConstantPdaSeedNode(b"creator-vault", encoding="utf8"),
            VariablePdaSeedNode("creator", source="account", encoding="pubkey"),
        ),
        program_id=PUMP,
    )
    try:
        with SurfpoolFork(mainnet) as fork:
            bc = verify_derivation(
                pdas["bonding_curve"], {"mint": MINT}, rpc_url=fork.rpc_url
            )
            gl = verify_derivation(pdas["global"], {}, rpc_url=fork.rpc_url)
            cv = verify_derivation(
                resolved_vault, {"creator": CREATOR}, rpc_url=fork.rpc_url
            )
            md = verify_derivation(
                pdas["metadata"], {"mint": MINT}, rpc_url=fork.rpc_url
            )
            abc = verify_derivation(
                pdas["associated_bonding_curve"],
                {"owner": BONDING_CURVE, "mint": MINT},
                rpc_url=fork.rpc_url,
            )
            # The REAL associated bonding curve on-chain: a TOKEN-2022 ATA. Same config
            # machinery (constant-pubkey seed) but with the Token-2022 program id — proves
            # ATA derivation reaches genuine on-chain state.
            abc_t22_node = node_from_spec(
                "associated_bonding_curve_t22",
                {
                    "program_id": ATA_PROGRAM,
                    "seeds": [
                        {
                            "kind": "variable",
                            "name": "owner",
                            "source": "account",
                            "encoding": "pubkey",
                        },
                        {
                            "kind": "constant",
                            "value": TOKEN_2022_PROGRAM,
                            "encoding": "pubkey",
                        },
                        {
                            "kind": "variable",
                            "name": "mint",
                            "source": "account",
                            "encoding": "pubkey",
                        },
                    ],
                },
            )
            abc_t22 = verify_derivation(
                abc_t22_node,
                {"owner": BONDING_CURVE, "mint": MINT},
                rpc_url=fork.rpc_url,
            )
    except SurfpoolError as exc:
        pytest.skip(f"surfpool fork unavailable: {exc}")

    # data accounts: derived address holds a real account OWNED BY the Pump program
    assert bc.address == BONDING_CURVE and bc.exists and bc.owner_matches
    assert gl.address == GLOBAL and gl.exists and gl.owner_matches
    # creator_vault is a SOL VAULT PDA — it exists and derives correctly, but it holds
    # lamports and is System-owned (the program signs for it via invoke_signed), so
    # owner is the System Program, not Pump. Deriving the right address is the proof.
    assert cv.address == CREATOR_VAULT and cv.exists
    assert (
        cv.owner == "11111111111111111111111111111111"
    )  # System Program (a SOL vault)

    # --- HONEST on-chain finding (see the ground-truth constants above) ---
    # The metadata + associated_bonding_curve recipes DERIVE the exact expected
    # addresses (proven offline, $0). But this fixture mint is a TOKEN-2022 mint, so
    # against real chain state:
    #   * metadata (a Metaplex Token Metadata PDA) does NOT exist — a Token-2022 mint
    #     stores its metadata as an in-mint extension, not a separate Metaplex account.
    #   * the classic-Token associated_bonding_curve (4cpMS2…) does NOT exist — the real
    #     ATA is derived under the Token-2022 program, not classic SPL Token.
    # We assert the derivations are ADDRESS-correct and record the real existence we
    # observed rather than forcing a green existence check on a phantom account.
    assert md.address == METADATA and not md.exists
    assert abc.address == ASSOCIATED_BONDING_CURVE and not abc.exists

    # POSITIVE on-chain proof that constant-pubkey + ATA derivation reaches real state:
    # the Token-2022 ATA of the bonding curve EXISTS and is OWNED BY the Token-2022
    # program (ATAs are owned by their token program, not the ATA program).
    assert abc_t22.address == ASSOCIATED_BONDING_CURVE_T22 and abc_t22.exists
    assert abc_t22.owner == TOKEN_2022_PROGRAM
