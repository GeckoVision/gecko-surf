"""``gecko prove`` — the front door, proven offline.

Both seams (the router and the simulator) are injected, so every path below runs with no
catalog, no network and no RPC. The behaviours that matter are the *refusals*: this
command is only useful if it is trusted, and it is only trusted if it is willing to
disappoint.
"""

from __future__ import annotations

from typing import Any

from gecko.find_start import (
    CatalogCandidate,
    DeriveStep,
    FindStartResult,
    GapSpec,
    StartPoint,
)
from gecko.prove import format_proof, prove
from gecko.simulate import Receipt


def _start(kind: str = "start", **over: Any) -> StartPoint:
    base: dict[str, Any] = {
        "kind": kind,
        "program": "pumpfun",
        "program_id": "6EF8rrec…",
        "instruction": "buy",
        "next_tool": "plan_buy",
        "score": 9,
        "why": ("buy", "pump"),
        "inputs": ("mint", "user", "amount"),
        "derive_plan": (
            DeriveStep(account="global", provenance="extracted"),
            DeriveStep(account="creator_vault", provenance="recovered"),
        ),
        "preludes": (),
        "gaps": (),
        "execute": None,
        "serve": "gecko-orquestra --program pumpfun",
    }
    base.update(over)
    return StartPoint(**base)


def _route(
    *starts: StartPoint, no_start: bool = False, catalog: tuple = ()
) -> FindStartResult:
    return FindStartResult(
        starts=tuple(starts),
        catalog=catalog,
        no_start=no_start,
        note="" if starts else "nothing matched",
    )


def _receipt(status: str = "pass") -> Receipt:
    return Receipt(
        status=status,
        err=None,
        revert_class="none" if status == "pass" else "account_error",
        units_consumed=86_669 if status == "pass" else 0,
        sol_delta=None,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
    )


class _Result:
    def __init__(self, receipt: Receipt) -> None:
        self.landing_receipt = receipt


# --------------------------------------------------------------------------- #
# The happy path — a sentence to a receipt.
# --------------------------------------------------------------------------- #
def test_an_intent_becomes_a_route_a_plan_and_a_receipt() -> None:
    result = prove(
        "buy this token on pump",
        bindings={"mint": "m", "user": "u", "amount": 1},
        router=lambda _intent, **_kw: _route(_start()),
        simulator=lambda *_: _Result(_receipt()),
    )

    assert result.start is not None and result.start.instruction == "buy"
    assert result.receipt is not None and result.receipt.status == "pass"
    assert result.provenance_counts == {"extracted": 1, "recovered": 1}


def test_the_rendering_shows_all_three_answers() -> None:
    result = prove(
        "buy this token on pump",
        bindings={"mint": "m"},
        router=lambda _intent, **_kw: _route(_start()),
        simulator=lambda *_: _Result(_receipt()),
    )

    text = "\n".join(format_proof(result))

    assert "pumpfun/buy" in text  # what to call
    assert "extracted" in text and "recovered" in text  # what it needs, and from where
    assert "86,669 compute units" in text  # whether it works
    assert "unsigned · $0" in text


# --------------------------------------------------------------------------- #
# The refusals — what makes it trustworthy.
# --------------------------------------------------------------------------- #
def test_nothing_matching_is_a_no_start_not_a_guess() -> None:
    result = prove(
        "make me a sandwich",
        router=lambda _intent, **_kw: _route(no_start=True),
        simulator=lambda *_: _Result(_receipt()),
    )

    assert result.start is None
    assert result.receipt is None
    assert "no start" in "\n".join(format_proof(result))


def test_a_guess_is_never_simulated() -> None:
    """A below-the-floor match answers 'where would I look', not 'what should I run'.
    Simulating it would turn an honest hedge into a fake certainty."""
    ran: list[int] = []

    result = prove(
        "something vague",
        router=lambda _intent, **_kw: _route(_start(kind="guess", score=2)),
        simulator=lambda *_: ran.append(1) or _Result(_receipt()),  # type: ignore[func-returns-value]
    )

    assert ran == []
    assert result.receipt is None
    assert "not a runnable plan" in result.reason


def test_a_surface_match_is_not_simulated_either() -> None:
    result = prove(
        "meteora",
        router=lambda _intent, **_kw: _route(_start(kind="surface", instruction=None)),
        simulator=lambda *_: _Result(_receipt()),
    )

    assert result.receipt is None


def test_a_flagged_account_is_surfaced_not_hidden() -> None:
    gap = GapSpec(name="fee_recipient", note="two valid candidates; we will not guess")
    result = prove(
        "buy",
        router=lambda _intent, **_kw: _route(_start(gaps=(gap,))),
        simulator=lambda *_: _Result(_receipt()),
    )

    assert "flagged: fee_recipient" in "\n".join(format_proof(result))


def test_a_reverting_simulation_reports_its_class() -> None:
    result = prove(
        "buy",
        router=lambda _intent, **_kw: _route(_start()),
        simulator=lambda *_: _Result(_receipt(status="fail")),
    )

    assert "FAIL   account_error" in "\n".join(format_proof(result))


# --------------------------------------------------------------------------- #
# Missing bindings — the orchestrator is the authority, not the declared input list.
# --------------------------------------------------------------------------- #
def test_the_orchestrators_own_requirement_message_is_surfaced() -> None:
    """`start.inputs` is a SUPERSET of what a caller must supply — pump quotes
    `max_sol_cost` from the curve rather than taking it — so pre-gating on it would refuse
    a call that runs. The orchestrator's own error is the honest one."""

    def refuses(*_args: Any) -> Any:
        raise ValueError("simulate_buy_landing needs bindings ['fee_recipient']")

    result = prove(
        "buy",
        bindings={"mint": "m"},
        router=lambda _intent, **_kw: _route(_start()),
        simulator=refuses,
    )

    assert "needs bindings ['fee_recipient']" in result.reason


def test_a_long_or_odd_failure_degrades_to_the_class_never_the_message() -> None:
    """An arbitrary exception message could carry a value, so only short
    requirement-shaped messages pass through."""

    def explodes(*_args: Any) -> Any:
        raise RuntimeError("secret-token-" + "x" * 200)

    result = prove(
        "buy",
        bindings={"mint": "m"},
        router=lambda _intent, **_kw: _route(_start()),
        simulator=explodes,
    )

    assert result.reason == "could not run (RuntimeError)"
    assert "secret-token" not in result.reason


def test_catalog_pointers_are_offered_when_nothing_is_wired() -> None:
    candidate = CatalogCandidate(
        slug="kamino-lend",
        name="kamino",
        program_id="KLend…",
        score=4,
        why=("lend",),
        comprehend_first={"tool": "comprehend_program"},
    )
    result = prove(
        "lend my sol",
        router=lambda _intent, **_kw: _route(no_start=True, catalog=(candidate,)),
    )

    assert "kamino" in "\n".join(format_proof(result))


# --------------------------------------------------------------------------- #
# The floor — refusing to guess is a feature, and it is regression-tested live.
# --------------------------------------------------------------------------- #
def test_a_single_shared_noun_does_not_produce_a_runnable_start() -> None:
    """Observed live before the fix: "get me out of hyUSD into USDC" routed to
    metadao/fund on the single word "usdc", score 2, and was served as a plan to RUN.
    A term that names no program and no action is not evidence."""
    from gecko.find_start import find_start

    result = find_start("get me out of hyUSD into USDC")

    assert result.no_start
    # Surface cards may still ride along ("this program is plausibly relevant"); what
    # must NOT appear is a runnable start.
    assert not any(start.kind == "start" for start in result.starts)


def test_naming_the_action_still_routes() -> None:
    """The floor must not become a wall — an intent that names a real action routes."""
    from gecko.find_start import find_start

    result = find_start("route a swap out of hyUSD")

    assert not result.no_start
    assert result.starts[0].kind == "start"
    assert result.starts[0].program == "jupiter"


def test_the_other_wired_programs_still_route() -> None:
    from gecko.find_start import find_start

    for intent, program in (
        ("buy this token on pump.fun", "pumpfun"),
        ("swap wsol to usdc on meteora", "meteora"),
        ("claim my ore rewards", "ore"),
    ):
        result = find_start(intent)
        assert not result.no_start, intent
        assert result.starts[0].program == program, intent


# --- the failure line a stranger actually reads -----------------------------------


def test_an_unreachable_rpc_names_the_url_and_the_next_action() -> None:
    """The first wall a new user hits. `prove` defaults to a LOCAL fork, so a machine
    without one used to get "could not run (RpcError)" — no cause, no next action, and no
    hint an RPC was even involved. In our flagship command."""
    from gecko.prove import _run_failure
    from gecko.rpc import LOCAL_RPC, RpcError

    line = _run_failure(RpcError("connection refused"), LOCAL_RPC)

    assert LOCAL_RPC in line
    assert "--rpc-url" in line
    assert "local fork default" in line


def test_it_says_the_routing_still_worked() -> None:
    """Routing is the part that DID succeed. A failure line that reads like total failure
    sends someone away from a command that just answered most of their question."""
    from gecko.prove import _run_failure
    from gecko.rpc import RpcError

    assert "routing above is complete" in _run_failure(RpcError("connection refused"))


def test_a_node_that_answered_with_an_error_reads_differently() -> None:
    """ "Nothing is listening" and "the node rejected your transaction" need opposite next
    actions, so they must not collapse into one message."""
    from gecko.prove import _run_failure
    from gecko.rpc import RpcError

    line = _run_failure(
        RpcError("JSON-RPC simulateTransaction failed: code=-32602 too large")
    )

    assert "RPC refused the simulation" in line
    assert "code=-32602" in line
    assert "--rpc-url" not in line


def test_an_unknown_exception_still_degrades_to_its_class() -> None:
    """RpcError is surfaced because it is DOCUMENTED to be payload-free. An arbitrary
    exception has made no such promise, so the redaction must still apply to it."""
    from gecko.prove import _run_failure

    assert (
        _run_failure(ValueError("secret-token-abc123")) == "could not run (ValueError)"
    )
    assert "abc123" not in _run_failure(ValueError("secret-token-abc123"))


def test_the_real_unreachable_case_is_not_an_rpcerror() -> None:
    """Found by running the command with no RPC: the stdlib raises a bare `URLError`,
    which escapes the RpcError wrapper. The first version of this fix matched on the
    class, looked right, and would have fixed nothing."""
    from urllib.error import URLError

    from gecko.prove import _run_failure
    from gecko.rpc import LOCAL_RPC

    line = _run_failure(URLError("[Errno 111] Connection refused"), LOCAL_RPC)

    assert LOCAL_RPC in line
    assert "--rpc-url" in line


def test_the_unreachable_branch_echoes_nothing() -> None:
    """That branch fires for ANY exception class, so it must not echo the message — the
    text is ours and the exception is only a signal. This is what keeps the redaction
    invariant intact while still being useful."""
    from gecko.prove import _run_failure

    line = _run_failure(
        RuntimeError("connection refused to https://user:sekret@rpc.x"), "http://a"
    )

    assert "sekret" not in line
    assert "no RPC at http://a" in line
