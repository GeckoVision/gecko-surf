"""The gecko-orquestra provider CLI — entry = provider, program = a parameter."""

from __future__ import annotations

import pytest

from gecko.providers.cli import PROGRAMS, main


def test_registry_has_meteora() -> None:
    assert "meteora" in PROGRAMS
    surface = PROGRAMS["meteora"]()
    # the registered builder yields a working surface
    out = surface.call_tool(
        "derive_pda",
        {
            "account": "lb_pair",
            "bindings": {
                "token_x_mint": "So11111111111111111111111111111111111111112",
                "token_y_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "bin_step": 4,
            },
        },
    )
    assert out["address"] == "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"


def test_program_is_required() -> None:
    with pytest.raises(SystemExit):
        main([])  # no --program


def test_unknown_program_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["--program", "nope"])  # not in the registry
