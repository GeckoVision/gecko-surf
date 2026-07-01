"""Report tests — the scorecard renderers + the control-plane-safe audit writer.

Thin presentation layer: given a real ``Scorecard`` (from an actual suite run) it renders a
console table, a JSON blob, and the naive-vs-defended headline, and forwards outcomes to the
allowlist-guarded corpus writer. Nothing here recomputes a score — it only presents.
"""

from __future__ import annotations

import json

from gecko.redteam import report
from gecko.redteam.harness import DefendedArm, NaiveArm, canary_session, run_suite
from gecko.redteam.policies import defended_policy, naive_policy
from gecko.redteam.scenarios import (
    ATTACKER_ADDR,
    CANARY_API_TOKEN,
    CANARY_JWT,
    CANARY_PRIVATE_KEY,
    SCENARIOS,
    U64_MAX,
)
from gecko.redteam.scorer import score

_ALLOWED_KEYS = {
    "ts",
    "scenario_id",
    "family",
    "tier",
    "layer",
    "vector",
    "polarity",
    "policy_id",
    "defenses",
    "verdict",
    "tripped_predicate",
    "blocked_reason",
    "leaked",
    "leak_sink",
    "auth_host_ok",
}


def _naive():
    outcomes = run_suite(
        SCENARIOS, naive_policy(SCENARIOS), NaiveArm(canary_session()), now=0
    )
    return outcomes, score(outcomes)


def _defended():
    outcomes = run_suite(
        SCENARIOS, defended_policy(SCENARIOS), DefendedArm(canary_session()), now=0
    )
    return outcomes, score(outcomes)


def test_render_text_shows_gate_and_cells():
    _, card = _defended()
    text = report.render(card, title="defended run")
    assert "defended run" in text
    # the 2x2 cells and the headline metrics are all present
    for token in ("blocked", "served", "Tier-0", "Tier-1", "FRR", "money_trusted"):
        assert token in text
    # a green run announces PASS, not FAIL
    assert "PASS" in text
    assert "money_trusted=True" in text


def test_render_text_naive_run_is_red():
    _, card = _naive()
    text = report.render(card, title="naive run")
    assert "FAIL" in text
    assert "money_trusted=False" in text


def test_render_json_roundtrips_the_scorecard():
    _, card = _defended()
    blob = json.loads(report.render_json(card))
    assert blob["tier0_asr"] == 0.0
    assert blob["money_trusted"] is True
    assert blob["cells"]["served"] == card.cells["served"]
    assert blob["n_attacks"] == card.n_attacks
    assert set(blob["asr_by_family"]) == set(card.asr_by_family)


def test_headline_shows_naive_to_defended_lift():
    _, naive_card = _naive()
    _, defended_card = _defended()
    line = report.render_headline(naive_card, defended_card)
    # the money shot: naive ASR high -> defended ASR 0, FRR flat
    assert "Tier-0" in line
    assert "FRR" in line
    assert "flat" in line.lower()
    assert "100%" in line  # naive tier-0 ASR
    assert "0%" in line  # defended tier-0 ASR


def test_write_audit_is_control_plane_safe(tmp_path):
    audit = tmp_path / "audit.jsonl"
    naive_outcomes, _ = _naive()
    defended_outcomes, _ = _defended()
    report.write_audit(naive_outcomes, str(audit))
    report.write_audit(defended_outcomes, str(audit))

    text = audit.read_text(encoding="utf-8")
    forbidden = [
        CANARY_JWT,
        CANARY_API_TOKEN,
        CANARY_PRIVATE_KEY,
        ATTACKER_ADDR,
        str(U64_MAX),
        "router",
        "evil.attacker.test",
        "trusted-ingest",
    ]
    for needle in forbidden:
        assert needle not in text, needle
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 24  # 12 scenarios x 2 arms
    for line in lines:
        assert set(json.loads(line)) == _ALLOWED_KEYS
