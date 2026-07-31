"""Phase 1 extraction: recover PdaNodes from real program SOURCE.

The killer test: a faithful slice of ORE's Steel source (no IDL) → extracted
PdaNodes → derive → the EXACT deployed mainnet addresses. Zero hand-coded seeds:
the whole `config`/`treasury`/`board` recipe is recovered from the text an Anchor
IDL would have dropped.
"""

from __future__ import annotations

from gecko.pda import ResolverPdaSeedNode, VariablePdaSeedNode, derive_pda
from gecko.pda_extract import extract_seed_consts, from_source

# ORE program id + ground-truth PDAs (api/src/consts.rs, mainnet).
ORE_PROGRAM = "oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv"
ORE_CONFIG = "9c9X7aDRAF41faiDs94ELjT19UrGnn72wBW9hPsS4Awy"
ORE_TREASURY = "45db2FSR4mcXdSVVZbKbwojU6uYDpMyhpEi7cC8nHaWG"
ORE_BOARD = "BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi"

# A faithful slice of ORE's Steel source: the const table (consts.rs) + the PDA
# accessors (state/mod.rs). This is exactly what Orquestra's IDL/llms.txt cannot
# express — and it is machine-regular.
ORE_SOURCE = r"""
pub const AUTOMATION: &[u8] = b"automation";
pub const BOARD: &[u8] = b"board";
pub const STATS: &[u8] = b"stats";
pub const CONFIG: &[u8] = b"config";
pub const MINER: &[u8] = b"miner";
pub const ROUND: &[u8] = b"round";
pub const TREASURY: &[u8] = b"treasury";

pub fn automation_pda(authority: Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[AUTOMATION, &authority.to_bytes()], &crate::ID)
}

pub fn board_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[BOARD], &crate::ID)
}

pub fn config_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[CONFIG], &crate::ID)
}

pub fn miner_pda(authority: Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[MINER, &authority.to_bytes()], &crate::ID)
}

pub fn round_pda(id: u64) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[ROUND, &id.to_le_bytes()], &crate::ID)
}

pub fn treasury_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[TREASURY], &crate::ID)
}

pub fn treasury_tokens_address() -> Pubkey {
    let treasury_address = treasury_pda().0;
    spl_associated_token_account::get_associated_token_address(&treasury_address, &MINT_ADDRESS)
}
"""


def test_extract_seed_consts() -> None:
    consts = extract_seed_consts(ORE_SOURCE)
    assert consts["CONFIG"] == b"config"
    assert consts["TREASURY"] == b"treasury"
    assert consts["MINER"] == b"miner"
    assert consts["ROUND"] == b"round"


def test_from_source_recovers_all_pda_accessors() -> None:
    nodes = from_source(ORE_SOURCE, program_id=ORE_PROGRAM)
    # every find_program_address accessor, keyed by account name (fn minus _pda)
    assert set(nodes) == {"automation", "board", "config", "miner", "round", "treasury"}
    # the ATA helper uses get_associated_token_address, NOT find_program_address —
    # correctly not surfaced as a PDA recipe.
    assert "treasury_tokens" not in nodes
    assert "treasury_tokens_address" not in nodes


def test_recovered_static_pdas_derive_to_mainnet_ground_truth() -> None:
    """Raw Rust source -> extracted PdaNode -> derived address == deployed mainnet
    constant. The seeds an Anchor IDL drops, recovered and proven correct."""
    nodes = from_source(ORE_SOURCE, program_id=ORE_PROGRAM)
    assert derive_pda(nodes["config"]).address == ORE_CONFIG
    assert derive_pda(nodes["treasury"]).address == ORE_TREASURY
    assert derive_pda(nodes["board"]).address == ORE_BOARD


def test_recovered_dynamic_pda_shapes() -> None:
    nodes = from_source(ORE_SOURCE, program_id=ORE_PROGRAM)

    miner = nodes["miner"]
    assert miner.resolvable
    assert miner.variable_seeds == (
        VariablePdaSeedNode("authority", source="account", encoding="pubkey"),
    )
    # and it derives when bound
    derived = derive_pda(miner, {"authority": ORE_CONFIG})
    assert len(derived.address) >= 32

    round_node = nodes["round"]
    assert round_node.variable_seeds == (
        VariablePdaSeedNode("id", source="argument", encoding="le", width=8),
    )
    assert (
        derive_pda(round_node, {"id": 7}).address
        != derive_pda(round_node, {"id": 8}).address
    )


def test_helper_fn_seed_becomes_honest_resolver() -> None:
    """The Anchor #4057 case: a seed that is a helper-function output cannot be
    statically resolved — it becomes a flagged ResolverPdaSeedNode with its deps,
    not a fabricated value and not a dropped account."""
    meteora_like = r"""
    pub const LB_PAIR: &[u8] = b"lb_pair";
    pub fn lb_pair_pda(token_x: Pubkey, token_y: Pubkey) -> (Pubkey, u8) {
        Pubkey::find_program_address(
            &[LB_PAIR, &max_key(token_x, token_y).to_bytes(), &min_key(token_x, token_y).to_bytes()],
            &crate::ID,
        )
    }
    """
    nodes = from_source(meteora_like, program_id=ORE_PROGRAM)
    lb_pair = nodes["lb_pair"]
    assert lb_pair.resolvable is False
    # the constant prefix is still recovered
    assert lb_pair.seeds[0].value == b"lb_pair"  # type: ignore[union-attr]
    # the max_key/min_key seeds are honestly flagged with their token deps
    resolvers = lb_pair.unresolved_seeds
    assert len(resolvers) == 2
    all_deps = {d for r in resolvers for d in r.depends_on}
    assert {"token_x", "token_y"} <= all_deps
    assert all(isinstance(r, ResolverPdaSeedNode) for r in resolvers)


def test_unknown_program_id_is_optional_until_derive() -> None:
    nodes = from_source(ORE_SOURCE)  # no program_id
    assert nodes["config"].program_id is None
    assert derive_pda(nodes["config"], program_id=ORE_PROGRAM).address == ORE_CONFIG
