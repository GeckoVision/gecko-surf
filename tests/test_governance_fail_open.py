"""The operator's spend cap must not be defeatable by the provider's choice of field name.

THE BUG THIS PINS. `_extract_amount` only looks at args whose name-tokens hit
`_CAP_AMOUNT_TOKENS` = {amount, price, total, cost, fee}. A provider naming the same field
`units`, `notional`, `lamports` or `value_minor` makes `_extract_amount` return None, and
`_cap_signal` then returns `[]` — no finding, no warning, nothing. The operator configured
a cap; the cap silently did not apply, and nobody was told.

Its comment called that "fail SAFE". It is fail-safe for a FINDING (we cannot assert
over-cap on an amount we cannot read) and fail-OPEN for a CONTROL. `gecko/spend_policy.py`
does the same job the other way: it keys on the mint ADDRESS and refuses
`amount-unresolvable` rather than falling silent.

THE FIX, and its deliberate narrowness. Emitting "the cap did not apply" on every write
without an amount arg would be noise — most writes move nothing. The dangerous case is
specific and detectable: the args DO carry a parseable amount, under a key we do not
recognise. That is exactly the bypass, and nothing else trips it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from gecko.ingest import Operation
from gecko.policy import AgentPolicy
from gecko.risk import RiskPolicy, classify_operation, score_call

GOV_POLICY = RiskPolicy(block_at=60, step_up_at=30)


def _transfer_op(properties: dict[str, Any] | None = None) -> Operation:
    """A transfer-tier op. Body props drive the tier vote, so they must look monetary."""
    props = properties if properties is not None else {
        "amount": {"type": "string"},
        "destination": {"type": "string"},
    }
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
                        "required": list(props),
                        "properties": props,
                    }
                }
            },
        },
        responses={},
    )


def _score(args: dict[str, Any], policy: AgentPolicy | None, op: Operation | None = None):
    operation = op or _transfer_op()
    return score_call(
        tool_name=operation.operation_id,
        tool_schema={},
        args=args,
        method=operation.method,
        tier=classify_operation(operation),
        agent_policy=policy,
        policy=GOV_POLICY,
    )


def _signals(assessment) -> set[str]:
    return {r.signal for r in assessment.reasons}


# ---------------------------------------------------------------------------
# the bypass
# ---------------------------------------------------------------------------


def test_an_unrecognised_amount_name_does_not_silently_void_the_cap() -> None:
    """`notional` is not in _CAP_AMOUNT_TOKENS, so the cap cannot be evaluated. The
    operator must learn that — silence is the bug."""
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    assessment = _score({"notional": "500.00", "destination": "0xGOOD"}, policy)
    assert "cap.unevaluated" in _signals(assessment), (
        "a parseable amount under an unrecognised key must report that the cap did not "
        "apply, not return no findings"
    )


def test_the_unevaluated_cap_carries_the_same_weight_as_an_exceeded_one() -> None:
    """We cannot prove the transfer is UNDER the cap, so on a confirmed transfer it must
    weigh what a known breach weighs. `spend_policy` refuses `amount-unresolvable` for the
    same reason."""
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    named = _score({"amount": "500.00", "destination": "0xGOOD"}, policy)
    unnamed = _score({"notional": "500.00", "destination": "0xGOOD"}, policy)
    assert unnamed.score == named.score
    assert unnamed.decision == named.decision


# ---------------------------------------------------------------------------
# and it must stay quiet everywhere else
# ---------------------------------------------------------------------------


def test_a_recognised_amount_under_the_cap_reports_nothing() -> None:
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    assessment = _score({"amount": "10.00", "destination": "0xGOOD"}, policy)
    assert "cap.unevaluated" not in _signals(assessment)
    assert "cap.exceeded" not in _signals(assessment)


def test_a_write_carrying_no_amount_at_all_is_not_flagged() -> None:
    """Most writes move nothing. Flagging every one of them would be noise, and noise is
    how a control gets switched off."""
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    assessment = _score({"reason": "customer requested", "destination": "0xGOOD"}, policy)
    assert "cap.unevaluated" not in _signals(assessment)


def test_no_cap_configured_means_nothing_to_report() -> None:
    assessment = _score({"notional": "500.00"}, AgentPolicy())
    assert "cap.unevaluated" not in _signals(assessment)


def test_a_read_is_never_flagged() -> None:
    """A GET moves no value; the cap has nothing to evaluate."""
    policy = AgentPolicy(spend_cap=Decimal("100"))
    op = Operation(
        method="get",
        path="/v1/transfers",
        operation_id="listTransfers",
        summary="List transfers",
        description="",
        tags=[],
        parameters=[],
        request_body=None,
        responses={},
    )
    assessment = _score({"notional": "500.00"}, policy, op=op)
    assert "cap.unevaluated" not in _signals(assessment)


def test_a_non_numeric_value_under_an_odd_key_is_not_an_amount() -> None:
    """The trigger is a PARSEABLE amount under an unrecognised key, not any unknown key."""
    policy = AgentPolicy(spend_cap=Decimal("100"), recipient_allowlist={"0xGOOD"})
    assessment = _score({"notional": "not-a-number", "destination": "0xGOOD"}, policy)
    assert "cap.unevaluated" not in _signals(assessment)


def test_a_benign_write_with_a_number_is_reported_but_does_not_move_the_score() -> None:
    """The noise case, measured before it shipped: at a 15-point weight a plain
    `createReport {"page_count": 42}` reached step_up (op.write 15 + 15 = 30). A control
    that fires on every write containing a number is a control someone switches off. Off a
    confirmed transfer this scores ZERO — visible in the reasons, silent in the score."""
    op = Operation(
        method="post",
        path="/v1/reports",
        operation_id="createReport",
        summary="Create a report",
        description="",
        tags=[],
        parameters=[],
        request_body={
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"page_count": {"type": "integer"}},
                    }
                }
            },
        },
        responses={},
    )
    policy = AgentPolicy(spend_cap=Decimal("100"))
    without = _score({}, policy, op=op)
    with_number = _score({"page_count": 42}, policy, op=op)

    assert "cap.unevaluated" in _signals(with_number), "the operator must still see it"
    assert with_number.score == without.score, "but it must not move a benign write"
    assert with_number.decision == without.decision
