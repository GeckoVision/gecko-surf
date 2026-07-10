"""Falsifier 1b — the governance gate (the headline claim).

Pure-Python, no-network. A fake upstream + a hand-authored transfer op + an ``AgentPolicy``
(spend cap + recipient allow-list) prove the intersection-blocks-only design:

1. a ``tier==transfer`` call OVER the spend cap -> block, upstream never called;
2. a ``tier==transfer`` call to a NON-allowlisted recipient -> block;
3. a ``tier==transfer`` call WITHIN cap + allowlisted -> allow/step_up, never block;
4. tier ALONE (no predicate) -> step_up, score < block_at (25 < 60) — tier never blocks alone;
5. ``cap.exceeded`` / ``recipient.not_allowlisted`` alone on a NON-transfer write -> step_up,
   never block (intersection-only).

The block comes from WEIGHT (transfer 25 + predicate 35 = 60 = block_at), never from adding a
governance signal to ``BLOCKING_SIGNALS`` — asserted here too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from gecko.enforce import apply_gate
from gecko.ingest import Operation
from gecko.policy import AgentPolicy
from gecko.risk import BLOCKING_SIGNALS, RiskPolicy, classify_operation, score_call

# A governed agent lowers step_up so any transfer at least WARNS (a sensible stance for a
# value-moving agent); block_at stays the default 60 so the intersection weight is the gate.
GOV_POLICY = RiskPolicy(step_up_at=20, block_at=60)


def _transfer_op() -> Operation:
    """A real comprehended transfer op — amount + destination in the body => tier=transfer."""
    return Operation(
        method="post",
        path="/v1/wallets/{wallet_id}/transfer",
        operation_id="transfer",
        summary="Send funds from a wallet",
        description="",
        tags=[],
        parameters=[],
        request_body={
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["amount", "destination"],
                        "properties": {
                            "amount": {"type": "string"},
                            "destination": {"type": "string"},
                        },
                    }
                }
            },
        },
        responses={},
        security=[],
    )


def _write_op() -> Operation:
    """A non-transfer write (create a note) — tier=write, no money-verb, no amount∧recipient."""
    return Operation(
        method="post",
        path="/v1/notes",
        operation_id="createNote",
        summary="Create a note",
        description="",
        tags=[],
        parameters=[],
        request_body={
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "string"}},
                    }
                }
            }
        },
        responses={},
        security=[],
    )


@dataclass
class FakeUpstream:
    """Records whether the real API was ever called (proves a block short-circuits it)."""

    called: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, args: dict[str, Any]) -> dict[str, Any]:
        self.called = True
        self.calls.append(args)
        return {"ok": True}


def _run(op: Operation, args: dict[str, Any], agent_policy: AgentPolicy | None):
    """Score -> gate -> only touch upstream if not blocked (the hosted call-path shape)."""
    upstream = FakeUpstream()
    assessment = score_call(
        tool_name=op.operation_id,
        tool_schema={},  # no declared props => schema signal stays silent (isolate governance)
        args=args,
        method=op.method,
        tier=classify_operation(op),
        agent_policy=agent_policy,
        policy=GOV_POLICY,
    )
    outcome = apply_gate(assessment, "block")
    if not outcome.blocked:
        upstream.call(args)
    return assessment, outcome, upstream


def test_transfer_over_cap_is_blocked_and_upstream_never_called() -> None:
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    a, gate, up = _run(
        _transfer_op(), {"amount": "500.00", "destination": "0xGOOD"}, policy
    )
    assert gate.blocked is True
    assert a.decision == "block"
    assert a.score >= 60
    assert up.called is False  # the steered over-cap transfer never reaches the API
    assert any(r.signal == "cap.exceeded" for r in a.reasons)


def test_transfer_to_non_allowlisted_recipient_is_blocked() -> None:
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    a, gate, up = _run(
        _transfer_op(), {"amount": "10.00", "destination": "0xEVIL"}, policy
    )
    assert gate.blocked is True
    assert a.decision == "block"
    assert up.called is False
    assert any(r.signal == "recipient.not_allowlisted" for r in a.reasons)


def test_transfer_within_cap_and_allowlisted_is_never_blocked() -> None:
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    a, gate, up = _run(
        _transfer_op(), {"amount": "10.00", "destination": "0xGOOD"}, policy
    )
    assert gate.blocked is False
    assert a.decision != "block"
    assert up.called is True
    assert not any(
        r.signal in ("cap.exceeded", "recipient.not_allowlisted") for r in a.reasons
    )


def test_tier_alone_never_blocks() -> None:
    # A transfer with NO governance predicate (no AgentPolicy) — tier is the only signal.
    a, gate, up = _run(
        _transfer_op(), {"amount": "10.00", "destination": "0xANY"}, None
    )
    assert gate.blocked is False
    assert a.decision == "step_up"
    assert a.score < 60  # 25 < block_at — tier never blocks on its own
    assert any(r.signal == "op.transfer" for r in a.reasons)
    assert up.called is True


def test_predicate_alone_on_non_transfer_write_only_steps_up() -> None:
    # An over-cap amount AND an off-allowlist recipient on a NON-transfer write. The predicates
    # fire (they gate on the arg-shape, not the tier) but WITHOUT transfer weight they cannot
    # reach block_at: write(15) + predicate(35) = 50 < 60 => step_up, never block.
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    write = _write_op()
    assert classify_operation(write).tier == "write"
    a, gate, up = _run(write, {"amount": "9999.00", "destination": "0xEVIL"}, policy)
    assert gate.blocked is False
    assert a.decision == "step_up"
    assert a.score < 60
    assert up.called is True
    # The predicate DID fire — it just wasn't load-bearing without the transfer tier.
    assert any(
        r.signal in ("cap.exceeded", "recipient.not_allowlisted") for r in a.reasons
    )


def test_unparseable_amount_fails_safe_to_step_up_not_block() -> None:
    # Amount extraction fails SAFE: if we cannot parse the amount we cannot assert over-cap,
    # so the transfer degrades to a step_up (tier only), never a block on a guess.
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    a, gate, up = _run(
        _transfer_op(), {"amount": "lots", "destination": "0xGOOD"}, policy
    )
    assert gate.blocked is False
    assert not any(r.signal == "cap.exceeded" for r in a.reasons)
    assert up.called is True


def test_governance_signals_are_not_categorical_blockers() -> None:
    # Seam-identity (§4.4): the governance signals must NEVER be members of BLOCKING_SIGNALS —
    # they block only by additive WEIGHT at the transfer intersection.
    assert "cap.exceeded" not in BLOCKING_SIGNALS
    assert "recipient.not_allowlisted" not in BLOCKING_SIGNALS
    assert "op.transfer" not in BLOCKING_SIGNALS
    assert BLOCKING_SIGNALS == frozenset(
        {"exfil.host", "poison.injection", "provenance.quarantined"}
    )
