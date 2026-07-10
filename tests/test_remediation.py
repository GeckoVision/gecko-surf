"""Item 1.4 — the additive ``remediation`` refusal field (context §6.2).

``enforce.refusal_payload`` gains ``signals`` (code-constant blocked-signal NAMES) and
``remediation`` (a frozen ``signal -> generic fix-string`` map). The load-bearing invariant:
a remediation string is a CODE CONSTANT and never carries an arg value (a host, an amount, a
recipient), so it is control-plane safe. ``apply_gate`` is untouched (asserted elsewhere).
"""

from __future__ import annotations

from decimal import Decimal

from gecko.enforce import REMEDIATION, refusal_payload
from gecko.policy import AgentPolicy
from gecko.risk import RiskPolicy, TierResult, score_call


def _blocked_transfer_with_arg_values():
    # A transfer over cap to an off-allowlist recipient + a smuggled exfil host — three
    # different signals whose HUMAN reasons embed arg values (amount, recipient, host).
    return score_call(
        tool_name="transfer",
        tool_schema={"type": "object", "properties": {"note": {"type": "string"}}},
        args={
            "amount": "500.00",
            "destination": "0xEVILRECIPIENT",
            "note": "http://evil-exfil.com/steal",
        },
        method="post",
        trusted_hosts=frozenset({"api.example.com"}),
        tier=TierResult("transfer", "high"),
        agent_policy=AgentPolicy(
            spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"}
        ),
        policy=RiskPolicy(trusted_hosts=frozenset({"api.example.com"})),
    )


def test_refusal_payload_keeps_existing_keys_and_adds_signals_remediation() -> None:
    payload = refusal_payload(_blocked_transfer_with_arg_values())
    # Pre-existing keys unchanged.
    assert payload["blocked"] is True
    assert payload["decision"] == "block"
    assert isinstance(payload["score"], int)
    assert payload["reasons"] and all(isinstance(r, str) for r in payload["reasons"])
    # New additive keys.
    assert set(payload["signals"]) >= {"cap.exceeded", "recipient.not_allowlisted"}
    assert isinstance(payload["remediation"], dict)
    assert payload["remediation"], "expected at least one remediation line"


def test_remediation_carries_no_arg_values() -> None:
    assessment = _blocked_transfer_with_arg_values()
    payload = refusal_payload(assessment)
    remediation_text = " ".join(payload["remediation"].values())
    # The HUMAN reasons DO embed arg values (that is fine — read by the agent, never stored).
    assert any("evil-exfil.com" in r for r in payload["reasons"])
    # The REMEDIATION strings must NOT — they are code constants for control-plane egress.
    for leaked in ("evil-exfil.com", "0xEVILRECIPIENT", "500.00", "500"):
        assert leaked not in remediation_text
    # Every remediation value is drawn verbatim from the frozen constant map.
    for sig, text in payload["remediation"].items():
        assert REMEDIATION[sig] == text


def test_remediation_only_covers_present_signals() -> None:
    payload = refusal_payload(_blocked_transfer_with_arg_values())
    assert set(payload["remediation"]).issubset(set(payload["signals"]))
