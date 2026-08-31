"""jurassic_fi's servable plan intent — derivation pinned to the mainnet-proven script.

scripts/jurassic_contribute.py is the module that simulated the live sale's contribute
end to end; its hand-derived addresses are the ground truth the config-driven plan must
reproduce. A drift between the two is a wrong-but-well-formed address — the exact class
of fault the thesis says must be caught by an independent derivation, so here it is.
"""

from __future__ import annotations

from gecko.providers.cli import PROGRAMS
from scripts.jurassic_contribute import (
    LAUNCH,
    USDC_MINT,
    associated_token_account,
    user_position,
)

CONTRIBUTOR = "GDDMwNyyx8uB6zrqwBFHjLLG3TBYk2F8Az4yrQC5RzMp"


def test_plan_contribute_matches_the_script_derivations() -> None:
    surface = PROGRAMS["jurassic_fi"]()
    plan = surface.call_tool(
        "plan_contribute",
        {"launch": LAUNCH, "contributor": CONTRIBUTOR, "payment_mint": USDC_MINT},
    )
    derived = plan["derived"]
    assert derived["user_position"] == user_position(LAUNCH, CONTRIBUTOR)
    assert derived["payment_vault"] == associated_token_account(LAUNCH, USDC_MINT)
    assert derived["contributor_payment_account"] == associated_token_account(
        CONTRIBUTOR, USDC_MINT
    )
    assert derived["launch"] == LAUNCH
    assert plan["instruction"] == "contribute"
    assert plan["execute"]["url"].endswith("/instructions/contribute/build")


def test_plan_contribute_refuses_missing_inputs() -> None:
    surface = PROGRAMS["jurassic_fi"]()
    out = surface.call_tool("plan_contribute", {"launch": LAUNCH})
    assert "error" in out
    assert "contributor" in out["error"] and "payment_mint" in out["error"]
