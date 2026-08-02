"""The config-driven provider backbone — PDA recipes and program identity as DATA.

See docs/specs/2026-08-01-provider-control-panel.md (§B0) and
docs/plans/2026-08-01-provider-control-panel-pr1-config-backbone.md.
"""

from __future__ import annotations

from gecko.pda import derive_pda
from gecko.provider_config import ConfigError, node_from_spec, seed_from_spec

METEORA = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

LB_PAIR_SPEC = {
    "program_id": METEORA,
    "seeds": [
        {"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "min"},
        {"kind": "ordered_pair", "left": "token_x_mint", "right": "token_y_mint", "select": "max"},
        {"kind": "variable", "name": "bin_step", "source": "argument", "encoding": "le", "width": 2},
    ],
}


def test_lb_pair_spec_derives_real_pool() -> None:
    node = node_from_spec("lb_pair", LB_PAIR_SPEC)
    got = derive_pda(node, {"token_x_mint": SOL, "token_y_mint": USDC, "bin_step": 4})
    assert got.address == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


def test_constant_utf8_seed_bytes() -> None:
    seed = seed_from_spec({"kind": "constant", "value": "oracle", "encoding": "utf8"})
    assert seed.value == b"oracle"


def test_unknown_seed_kind_rejected() -> None:
    try:
        seed_from_spec({"kind": "nope"})
        assert False, "expected ConfigError"
    except ConfigError:
        pass
