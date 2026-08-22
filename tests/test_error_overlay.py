"""Gecko's advice about a program error, and the line it must never cross."""

from __future__ import annotations

from gecko.error_overlay import LET_ME_BUY, Remediation, remediation_for
from gecko.program_errors import name_program_error

IDL = {
    "errors": [
        {"code": 6008, "name": "VectorLimitReached", "msg": "VectorLimitReached"}
    ]
}
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def test_the_cap_error_carries_earned_guidance() -> None:
    advice = remediation_for(LET_ME_BUY, 6008)
    assert isinstance(advice, Remediation)
    assert "20 products" in advice.guidance
    assert "COUNT, not a byte budget" in advice.guidance
    # Every entry says how it came to be known, so a reader can weigh it.
    assert "measured on a fork" in advice.established_by


def test_advice_is_labelled_as_ours_and_never_as_the_programs() -> None:
    """The two voices must stay separable: the name is the program's, this is not."""
    advice = remediation_for(LET_ME_BUY, 6008)
    assert advice is not None and advice.source == "gecko-overlay"
    named = name_program_error(
        {"InstructionError": [0, {"Custom": 6008}]},
        logs=(f"Program {LET_ME_BUY} failed: custom program error: 0x1778",),
        idl=IDL,
        program_id=LET_ME_BUY,
    )
    assert named is not None and named.source == "program-idl"
    assert named.source != advice.source


def test_an_unknown_program_gets_no_advice_rather_than_generic_advice() -> None:
    """6008 means something in let_me_buy. It means nothing here, and we say nothing."""
    assert remediation_for(TOKEN, 6008) is None
    assert remediation_for(LET_ME_BUY, 6999) is None
    assert remediation_for(None, 6008) is None
    assert remediation_for(LET_ME_BUY, None) is None
