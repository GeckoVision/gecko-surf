"""The enforcement dispatch lives in ONE place — ``enforce.apply_gate`` — so the
single-surface and multi-surface hosts can never diverge on what a decision DOES.

``apply_gate`` turns a scored ``RiskAssessment`` + a mode into a transport-agnostic
``GateOutcome`` (block? warn?). ``resolve_hosted_enforce`` is the single place the hosted
serving default is resolved.
"""

from __future__ import annotations

from gecko.enforce import (
    GateOutcome,
    apply_gate,
    resolve_hosted_enforce,
)
from gecko.risk import RiskAssessment, Reason


def _assessment(decision: str, score: int = 60) -> RiskAssessment:
    return RiskAssessment(
        score=score,
        decision=decision,  # type: ignore[arg-type]
        reasons=[Reason("poison.injection", score, "x")],
    )


def test_block_mode_blocks_a_decided_block() -> None:
    out = apply_gate(_assessment("block"), "block")
    assert isinstance(out, GateOutcome)
    assert out.blocked is True
    assert out.warn is False


def test_warn_mode_never_blocks_but_flags_a_would_be_block() -> None:
    out = apply_gate(_assessment("block"), "warn")
    assert out.blocked is False
    assert out.warn is True  # surfaced, not hidden


def test_block_mode_step_up_executes_with_warning() -> None:
    out = apply_gate(_assessment("step_up", score=35), "block")
    assert out.blocked is False
    assert out.warn is True


def test_allow_is_clean() -> None:
    out = apply_gate(_assessment("allow", score=0), "block")
    assert out.blocked is False
    assert out.warn is False


def test_off_or_none_is_passthrough() -> None:
    assert apply_gate(_assessment("block"), "off").blocked is False
    assert apply_gate(None, "block").blocked is False
    assert apply_gate(None, "block").warn is False


def test_hosted_default_is_single_sourced(monkeypatch) -> None:
    # ONE place resolves the hosted default; both hosted servers call it, so single- and
    # multi-surface can never default to different stances.
    monkeypatch.delenv("GECKO_ENFORCE", raising=False)
    assert resolve_hosted_enforce() == "block"  # hosted default
    assert resolve_hosted_enforce("warn") == "warn"  # explicit wins
    monkeypatch.setenv("GECKO_ENFORCE", "off")
    assert resolve_hosted_enforce() == "off"  # env override
    monkeypatch.setenv("GECKO_ENFORCE", "garbage")
    assert resolve_hosted_enforce() == "block"  # invalid -> fail-safe hosted default
