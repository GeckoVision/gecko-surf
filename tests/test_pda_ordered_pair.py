"""The ordered-pair seed kind — derive the min/max pool-pair seed the IDL drops (#4057).

Closes the flagship gap: Meteora's `lb_pair` seed is `[min(x,y), max(x,y), bin_step]`,
a helper-function ordering Anchor silently omits. This proves Gecko both EXTRACTS it from
source (as a resolvable OrderedPairPdaSeedNode, not a flagged resolver) and DERIVES it to
the real mainnet SOL-USDC pool.
"""

from __future__ import annotations

import pytest

from gecko.pda import (
    OrderedPairPdaSeedNode,
    PdaNode,
    VariablePdaSeedNode,
    derive_pda,
)
from gecko.pda_extract import from_source

DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LB_PAIR = (
    "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"  # real mainnet SOL-USDC DLMM pool
)
BINDINGS = {"token_x_mint": WSOL, "token_y_mint": USDC, "bin_step": 4}


def _lbpair_node() -> PdaNode:
    return PdaNode(
        name="lb_pair",
        seeds=(
            OrderedPairPdaSeedNode("token_x_mint", "token_y_mint", "min"),
            OrderedPairPdaSeedNode("token_x_mint", "token_y_mint", "max"),
            VariablePdaSeedNode("bin_step", source="argument", encoding="le", width=2),
        ),
        program_id=DLMM,
    )


def test_ordered_pair_node_is_resolvable_and_derives_real_pool() -> None:
    node = _lbpair_node()
    assert node.resolvable is True  # NOT a flagged resolver — this one we can derive
    assert node.required_bindings == ("token_x_mint", "token_y_mint", "bin_step")
    assert derive_pda(node, BINDINGS).address == LB_PAIR


def test_min_and_max_select_different_ends() -> None:
    lo_node = PdaNode("x", (OrderedPairPdaSeedNode("a", "b", "min"),), program_id=DLMM)
    hi_node = PdaNode("x", (OrderedPairPdaSeedNode("a", "b", "max"),), program_id=DLMM)
    b = {"a": WSOL, "b": USDC}
    assert derive_pda(lo_node, b).address != derive_pda(hi_node, b).address
    # order of operands must not matter — sorting is by bytes, not argument position
    assert (
        derive_pda(lo_node, {"a": USDC, "b": WSOL}).address
        == derive_pda(lo_node, b).address
    )


def test_missing_operand_raises() -> None:
    from gecko.pda import MissingBindingError

    node = _lbpair_node()
    with pytest.raises(MissingBindingError):
        derive_pda(node, {"token_x_mint": WSOL, "bin_step": 4})  # no token_y_mint


# --- extraction: recover the seed from the real Meteora source -------------

METEORA_SOURCE = r"""
pub fn derive_lb_pair_pda(
    token_x_mint: Pubkey,
    token_y_mint: Pubkey,
    bin_step: u16,
) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[
            min(token_x_mint, token_y_mint).as_ref(),
            max(token_x_mint, token_y_mint).as_ref(),
            &bin_step.to_le_bytes(),
        ],
        &dlmm::ID,
    )
}
"""


def test_from_source_recovers_ordered_pair_and_derives_real_pool() -> None:
    """Raw Meteora source (the min/max ordering) → resolvable PdaNode → derives to the
    exact real mainnet pool. The seed Orquestra shows blank, recovered AND derivable."""
    nodes = from_source(METEORA_SOURCE, program_id=DLMM)
    node = nodes["derive_lb_pair"]  # fn name minus the `_pda` suffix
    assert node.resolvable is True
    assert node.seeds[0] == OrderedPairPdaSeedNode(
        "token_x_mint", "token_y_mint", "min"
    )
    assert node.seeds[1] == OrderedPairPdaSeedNode(
        "token_x_mint", "token_y_mint", "max"
    )
    assert derive_pda(node, BINDINGS).address == LB_PAIR


def test_anchor_max_key_form_parses() -> None:
    """Anchor's `max_key(&a, &b)` / `min_key(&a, &b)` (with refs) also parse."""
    src = r"""
    pub fn derive_pool(a: Pubkey, b: Pubkey) -> (Pubkey, u8) {
        Pubkey::find_program_address(
            &[min_key(&a, &b).as_ref(), max_key(&a, &b).as_ref()],
            &crate::ID,
        )
    }
    """
    node = from_source(src, program_id=DLMM)["derive_pool"]
    assert node.resolvable is True
    assert [s.select for s in node.seeds] == ["min", "max"]  # type: ignore[union-attr]
