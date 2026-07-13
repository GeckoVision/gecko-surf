"""Task 5.2 — the offline gate for the self-heal agent loop.

The runner (``scripts/selfheal_eval.py``) makes real Haiku calls; this proves the harness
mechanics with a SCRIPTED fake LLM, fully deterministic and $0:
  * the correlated SimWorld gate really fires through the loop (naive withdraw -> 422,
    deposit-then-withdraw -> 200);
  * the two arms differ ONLY in query_docs presence + remediation reveal;
  * no wire / no ``client.call`` is ever reached (probe is the transport edge);
  * the aggregation (success rate, iters-to-heal, hallucination via the Phase-0 metric,
    variance) reads the records correctly.
"""

from __future__ import annotations

from typing import Any

from gecko.fcc_eval import hallucination_rate
from gecko.selfheal_eval import (
    DEPOSIT_OP,
    WITHDRAW_OP,
    ProbeDispatcher,
    SelfHealRecord,
    arm_tool_defs,
    escrow_client,
    escrow_operations,
    escrow_tasks,
    mean_iters_to_heal,
    multi_step_lift,
    multi_step_success_rate,
    run_episode,
    success_variance,
)

# --- a scripted fake LLM (the injected seam) -------------------------------------------


class _Block:
    def __init__(self, name: str, args: dict[str, Any], bid: str = "tu") -> None:
        self.type = "tool_use"
        self.name = name
        self.input = args
        self.id = bid


class _Resp:
    def __init__(self, blocks: list[Any], stop: str = "tool_use") -> None:
        self.content = blocks
        self.stop_reason = stop


class _Messages:
    def __init__(self, script: list[_Resp]) -> None:
        self._script = script
        self._i = 0
        self.seen: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Resp:
        self.seen.append(kwargs)
        resp = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return resp


class _FakeLLM:
    def __init__(self, script: list[_Resp]) -> None:
        self.messages = _Messages(script)


def _dispatcher(reveal: bool, presented: set[str]) -> ProbeDispatcher:
    return ProbeDispatcher(
        escrow_client(),
        escrow_operations(),
        presented,
        reveal_remediation=reveal,
    )


# --- the SimWorld gate really fires through the loop -----------------------------------


def test_naive_single_withdraw_fails_the_correlated_flow() -> None:
    ops = escrow_operations()
    defs, presented = arm_tool_defs(ops, include_query_docs=False)
    # The model just calls withdraw once, then ends its turn.
    llm = _FakeLLM(
        [
            _Resp([_Block(WITHDRAW_OP, {"amount": 50})]),
            _Resp([], stop="end_turn"),
        ]
    )
    outcome = run_episode(
        llm,
        _dispatcher(False, presented),
        defs,
        "release 50",
        WITHDRAW_OP,
        model="fake",
    )
    assert outcome.multi_step_ok is False
    assert outcome.heal_iter is None


def test_deposit_then_withdraw_completes_the_correlated_flow() -> None:
    ops = escrow_operations()
    defs, presented = arm_tool_defs(ops, include_query_docs=True)
    # iter1: naive withdraw -> 422; iter2: deposit then withdraw in one turn -> 200.
    llm = _FakeLLM(
        [
            _Resp([_Block(WITHDRAW_OP, {"amount": 50}, "a")]),
            _Resp(
                [
                    _Block(DEPOSIT_OP, {"amount": 50}, "b"),
                    _Block(WITHDRAW_OP, {"amount": 50}, "c"),
                ]
            ),
        ]
    )
    outcome = run_episode(
        llm, _dispatcher(True, presented), defs, "release 50", WITHDRAW_OP, model="fake"
    )
    assert outcome.multi_step_ok is True
    assert outcome.heal_iter == 2  # succeeded on the second loop pass
    assert outcome.healed is True  # succeeded AFTER a real failure


def test_lucky_deposit_first_succeeds_without_healing() -> None:
    ops = escrow_operations()
    defs, presented = arm_tool_defs(ops, include_query_docs=False)
    llm = _FakeLLM(
        [
            _Resp(
                [
                    _Block(DEPOSIT_OP, {"amount": 50}, "b"),
                    _Block(WITHDRAW_OP, {"amount": 50}, "c"),
                ]
            )
        ]
    )
    outcome = run_episode(
        llm,
        _dispatcher(False, presented),
        defs,
        "release 50",
        WITHDRAW_OP,
        model="fake",
    )
    assert outcome.multi_step_ok is True
    assert outcome.healed is False  # no failure preceded the success


# --- the arms differ only where they should --------------------------------------------


def test_selfheal_arm_reveals_remediation_baseline_does_not() -> None:
    ops = escrow_operations()
    _, presented = arm_tool_defs(ops, include_query_docs=True)
    reply_full = _dispatcher(True, presented).call(WITHDRAW_OP, {"amount": 50})
    reply_bare = _dispatcher(False, presented).call(WITHDRAW_OP, {"amount": 50})
    assert reply_full.status == 422 and reply_bare.status == 422
    assert (
        "remediation" in reply_full.content
        and "state.insufficient" in reply_full.content
    )
    assert "remediation" not in reply_bare.content  # baseline is told THAT, never WHY


def test_query_docs_only_presented_to_selfheal_arm() -> None:
    ops = escrow_operations()
    _, base_names = arm_tool_defs(ops, include_query_docs=False)
    _, heal_names = arm_tool_defs(ops, include_query_docs=True)
    assert "query_docs" not in base_names
    assert "query_docs" in heal_names


def test_query_docs_dispatch_surfaces_the_fix_and_stays_control_plane() -> None:
    ops = escrow_operations()
    _, presented = arm_tool_defs(ops, include_query_docs=True)
    reply = _dispatcher(True, presented).call(
        "query_docs", {"intent": "how to withdraw"}
    )
    assert reply.known is True
    assert "inputSchema" in reply.content  # the fix contract is surfaced
    assert "_invoke" not in reply.content  # no routing/auth leaks (invariant #4)


def test_unpresented_tool_name_is_flagged_as_hallucination() -> None:
    ops = escrow_operations()
    _, presented = arm_tool_defs(ops, include_query_docs=False)
    # query_docs is NOT presented to the baseline arm -> calling it is an invented op.
    reply = _dispatcher(False, presented).call("query_docs", {"intent": "x"})
    assert reply.known is False


# --- no wire ever reached (probe is the transport edge) --------------------------------


def test_dispatch_never_reaches_the_live_transport() -> None:
    wire: list[Any] = []

    def transport(req: Any) -> tuple[int, Any]:
        wire.append(req)
        return 200, {}

    from gecko.client import AgentApiClient
    from gecko.selfheal_eval import ESCROW_SPEC

    client = AgentApiClient(
        ESCROW_SPEC, base_url="https://escrow.example.com", live_transport=transport
    )
    ops = escrow_operations()
    _, presented = arm_tool_defs(ops, include_query_docs=True)
    disp = ProbeDispatcher(client, ops, presented, reveal_remediation=True)
    disp.call(WITHDRAW_OP, {"amount": 50})
    disp.call(DEPOSIT_OP, {"amount": 50})
    disp.call(WITHDRAW_OP, {"amount": 50})
    disp.call("query_docs", {"intent": "withdraw"})
    assert wire == []  # the sandbox sits on the no-wire side of the transport edge


# --- aggregation reads the records correctly (incl. the reused Phase-0 metric) ---------


def _records() -> list[SelfHealRecord]:
    return [
        SelfHealRecord("g", "baseline", 0, False, None, 1, False, False),
        SelfHealRecord("g", "baseline", 1, False, None, 1, True, False),
        SelfHealRecord("g", "selfheal", 0, True, 2, 3, False, True),
        SelfHealRecord("g", "selfheal", 1, True, 3, 4, False, True),
    ]


def test_aggregation_metrics() -> None:
    recs = _records()
    assert multi_step_success_rate(recs, "baseline") == 0.0
    assert multi_step_success_rate(recs, "selfheal") == 1.0
    assert multi_step_lift(recs) == 1.0
    assert mean_iters_to_heal(recs, "selfheal") == 2.5
    assert mean_iters_to_heal(recs, "baseline") is None
    # the reused Phase-0 metric scores the SelfHealRecord directly (needs .arm + .hallucinated)
    assert hallucination_rate(recs, "baseline") == 0.5
    assert hallucination_rate(recs, "selfheal") == 0.0
    mean, stdev = success_variance(recs, "selfheal")
    assert mean == 1.0 and stdev == 0.0


def test_escrow_golden_is_a_small_correlated_set() -> None:
    tasks = escrow_tasks()
    assert 2 <= len(tasks) <= 4
    for t in tasks:
        assert t.prereq_op == DEPOSIT_OP and t.settle_op == WITHDRAW_OP
