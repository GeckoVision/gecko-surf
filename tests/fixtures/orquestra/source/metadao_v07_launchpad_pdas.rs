// PDA seed recipes for MetaDAO launchpad_v07 (moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM).
//
// The uploaded IDL declares NO pda.seeds at all (Orquestra's PDA Finder reports
// "No PDA accounts found in this IDL"); the recipes exist only as
// `#[account(seeds = [...])]` constraints in the v07 source. Transcribed here to
// accessor form from metadao's v07_launchpad program source (verified against
// real mainnet accounts — see the packaged config notes). The seeds are public
// on-chain constants; this slice is comprehension INPUT, caller-supplied under
// the source-trust boundary (founder-curated).

pub const LAUNCH: &[u8] = b"launch";
pub const LAUNCH_SIGNER: &[u8] = b"launch_signer";
pub const FUNDING_RECORD: &[u8] = b"funding_record";

pub fn launch_pda(base_mint: Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[LAUNCH, &base_mint.to_bytes()], &crate::ID)
}

pub fn launch_signer_pda(launch: Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[LAUNCH_SIGNER, &launch.to_bytes()], &crate::ID)
}

pub fn funding_record_pda(launch: Pubkey, funder: Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[FUNDING_RECORD, &launch.to_bytes(), &funder.to_bytes()], &crate::ID)
}
