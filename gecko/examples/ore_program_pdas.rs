// PDA seed recipes for the ORE program (oreV3EG1i9BEgiAJ8b177Z2S2rMarzak4NMv1kULvWv).
//
// Transcribed from regolith-labs/ore (Apache-2.0) — api/src/consts.rs (the seed-byte
// constants) and api/src/state/mod.rs (the `*_pda` accessors). ORE is a Steel-framework
// program: it emits NO Anchor IDL, so these seed recipes exist ONLY in the source. This
// bundled slice lets Gecko comprehend them at serve time and hand an agent the exact
// derivation an IDL/llms.txt cannot express. The seeds are public on-chain constants.

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

pub fn stats_pda() -> (Pubkey, u8) {
    Pubkey::find_program_address(&[STATS], &crate::ID)
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
