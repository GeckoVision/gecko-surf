// PDA seed recipes for Meteora DLMM (LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo)
// that Anchor's IDL macro DROPS (#4057) — the helper-seeded roots.
//
// Transcribed to accessor form from MeteoraAg/dlmm-sdk (commons/src/pda.rs):
//  - `derive_lb_pair_pda2`: the CURRENT 4-seed lb_pair scheme
//    [min(mints), max(mints), bin_step (u16 LE), base_factor (u16 LE)] — the SDK
//    deprecated the 3-seed scheme in PR #49; base_factor is a plain caller
//    argument (the fee tier), NOT a PresetParameter read.
//  - `derive_reserve_pda`: [lb_pair, token_mint].
// Binding names align with the program IDL's account names (token_x_mint /
// token_y_mint) so IDL- and source-recovered recipes join on the same graph.
// The seeds are public on-chain constants; this slice is comprehension INPUT,
// caller-supplied under the source-trust boundary (founder-curated).

pub fn lb_pair_pda(token_x_mint: Pubkey, token_y_mint: Pubkey, bin_step: u16, base_factor: u16) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[
            min(token_x_mint, token_y_mint).as_ref(),
            max(token_x_mint, token_y_mint).as_ref(),
            &bin_step.to_le_bytes(),
            &base_factor.to_le_bytes(),
        ],
        &crate::ID,
    )
}

pub fn reserve_pda(lb_pair: Pubkey, token_mint: Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[&lb_pair.to_bytes(), &token_mint.to_bytes()], &crate::ID)
}
