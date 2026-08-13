"""The autonomous-signing demo — four refusals, one signature, and no bytes on any refusal.

WHAT IS ASSERTED HERE, AND WHY IT IS NOT "the verdict was False". Every refusal test below
checks THREE things: that the verdict refused, that ``bytes_out is None``, and that the
signing backend was reached ``0`` times. A refusal that returns ``approved=False`` beside a
populated payload is a label, not a control; and one that reaches the signer before
deciding has already handed the bytes over. Only the party at the far end of the seam can
testify to that, so the demo's spy counts its calls and these tests read the count.

PATTERN B, AND THIS PROJECT HAS BEEN BURNED BY IT. Scenarios (a)-(d) are proved with NO
validator and NO network — one test even breaks the HTTP transport for the duration to
prove it. The fork leg is separate, is skipped LOUDLY (with the reason printed) when
surfpool is absent, and can never stand in for the offline ones: a release must not pass
because a validator happened to be running on the machine.

MUTATION CHECKS. Two tests are named after the mutation they exist to catch — moving the
quarantine gate after ``simulate``, and moving the spend policy after the signer. Both
mutations were performed against these tests and both turned them red; the verbatim runs
are in the commit message.

NOTHING HERE SIGNS OR BROADCASTS ON ANY NETWORK. The happy path "signs" with a fake
backend that holds no key and writes a 64-byte ``0xEE`` marker into the signature slot.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from examples.autonomous_signing_demo import (
    PAYER,
    SIGNATURE_FEE_LAMPORTS,
    SIGNATURE_MARKER,
    AttemptSpec,
    DemoLeg,
    ScenarioResult,
    fork_leg,
    main,
    recorded_leg,
    render,
    run_attempt,
    run_scenarios,
    scenarios,
)
from gecko.pda_testkit import SurfpoolError, SurfpoolFork, surfpool_status

#: A port the demo's fork leg owns, deliberately NOT 8899. A test that would accept
#: whatever answers on the default port inherits an ambient validator's state, which is
#: the same bug as passing because surfpool happened to be running.
FORK_PORT = 8937


def _offline(spec: AttemptSpec) -> ScenarioResult:
    """One scenario on the recorded leg — no validator, no network."""
    return run_attempt(recorded_leg(spec.lamports + SIGNATURE_FEE_LAMPORTS), spec)


def _spec(key: str) -> AttemptSpec:
    for candidate in scenarios():
        if candidate.key == key:
            return candidate
    raise AssertionError(f"no scenario {key!r}")


# ------------------------------------------------------------ (a) the over-limit refusal


def test_a_an_over_limit_transaction_is_refused_by_the_spend_policy() -> None:
    result = _offline(_spec("a"))

    assert result.refused is True
    assert result.bytes_out is None, "a refusal that yields bytes is not a refusal"
    assert result.signer_calls == 0, "the signing backend must never have been reached"
    assert result.code == "over-per-transaction-cap"
    assert "spend_policy" in result.control
    assert "per-transaction bound" in result.reason


def test_the_spend_policy_runs_BEFORE_THE_SIGNER_and_moving_it_after_fails_this() -> (
    None
):
    """MUTATION CHECK. Move the ``policy_gate.authorize`` block in ``run_attempt`` below
    the ``signer.sign`` call and this test goes red on ``signer_calls``.

    It is worth a dedicated test because the ordering is NOT enforced by any type:
    ``TransactionSigner.sign`` takes no ``SpendVerdict``, so nothing stops a caller from
    signing first and asking about the budget afterwards. The result would still print
    "REFUSED" — with a signature already produced. The count is the only witness.
    """
    result = _offline(_spec("a"))

    assert result.signer_calls == 0
    assert result.builder_calls == 1, (
        "the builder IS reached here on purpose — the policy is evaluated over the "
        "DECODED bytes, so there is a transaction to decode. Only the signer is off-limits."
    )


# ---------------------------------------------------- (b) the fork/mainnet network control


def test_b_a_fork_receipt_is_refused_against_a_mainnet_expectation() -> None:
    """#366's control. The receipt was taken on a fork; the signature is claimed to be
    headed for mainnet. A snapshot of one network attests nothing about the other, and the
    refusal reads the STRUCTURED network field — never the prose label, which literally
    contains the word "mainnet" on every fork receipt."""
    result = _offline(_spec("b"))

    assert result.refused is True
    assert result.bytes_out is None
    assert result.signer_calls == 0
    assert "fork" in result.reason and "mainnet" in result.reason
    assert result.policy_authorized is True, (
        "the spend policy passed — this refusal is the network control's, and reading it "
        "as a policy refusal would hide that the two predicates are independent"
    )


# ------------------------------------------------------------- (c) the quarantine refusal


def test_c_a_poisoned_tool_is_refused_before_the_builder_is_ever_reached() -> None:
    result = _offline(_spec("c"))

    assert result.refused is True
    assert result.bytes_out is None
    assert result.signer_calls == 0
    assert result.builder_calls == 0, "a quarantined tool must not even be built"
    assert "quarantined" in result.reason


def test_the_quarantine_gate_runs_BEFORE_SIMULATE_and_moving_it_after_fails_this(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MUTATION CHECK. Move ``gate_surface_tool`` in ``gecko.handoff.prepare_handoff``
    below the ``simulate`` call and this test goes red on the tripwire.

    Asserting the RESULT is a refusal would not catch that mutation: a gate bolted on
    after the simulation also produces a refusal — having already built the transaction,
    spent the round trip, and MINTED A PASSING RECEIPT for a poisoned tool. That Receipt
    is a portable artifact ``prepare_handoff`` hands out, so it outlives the refusal.
    """
    reached: list[str] = []

    def tripwire(*_args: Any, **_kwargs: Any) -> Any:
        reached.append("simulate")
        raise AssertionError("simulate was reached for a tool the gate must refuse")

    monkeypatch.setattr("gecko.handoff.simulate", tripwire)
    result = _offline(_spec("c"))

    assert reached == [], "the gate must run BEFORE simulate, not after it"
    assert result.refused is True
    assert result.bytes_out is None


# ---------------------------------------------------- (d) the substituted-transaction case


def test_d_bytes_that_differ_from_what_was_simulated_are_refused() -> None:
    """NOT a happy path that returns True. The subject really is a different transaction:
    same program, same selector, same destination, same fee payer, 100× the lamports. It
    decodes cleanly and it is refused solely because the receipt does not attest it."""
    result = _offline(_spec("d"))

    assert result.refused is True
    assert result.bytes_out is None
    assert result.signer_calls == 0
    assert "NOT the one the receipt attests" in result.reason


def test_d_the_spend_policy_authorised_the_substituted_bytes_and_the_binding_did_not() -> (
    None
):
    """The reason (d) is a distinct scenario rather than a variant of the allowlist one.

    The substituted transaction passes EVERY authorization cap: its program and selector
    are allowlisted, its only writable destination is allowlisted, and the per-transaction
    cap is read from the RECEIPT's ``sol_delta`` — which describes the small transfer that
    was simulated, not the large one now in front of the signer. Authorization cannot see
    the swap. Only the binding can. If this assertion ever flips to ``False``, the scenario
    has silently become a duplicate of (a) and stops proving anything.
    """
    result = _offline(_spec("d"))

    assert result.policy_authorized is True
    assert result.refused is True


# -------------------------------------------------------------------- (e) the happy path


def test_e_the_happy_path_signs_autonomously_with_no_human_in_the_loop() -> None:
    result = _offline(_spec("e"))

    assert result.refused is False
    assert result.bytes_out is not None
    assert result.signer_calls == 1
    assert "exact" in result.reason
    assert "network=fork" in result.reason


def test_e_the_signature_is_a_marker_written_by_a_backend_that_holds_no_key() -> None:
    """C13. The happy path proves the DECISION path is autonomous and correct. It does not
    prove a signature: the backend holds nothing and writes ``0xEE``×64 into the signature
    slot. A fake that looked real is how a demo gets screenshotted as a mainnet
    transaction."""
    import base64

    result = _offline(_spec("e"))
    assert result.bytes_out is not None
    raw = base64.b64decode(result.bytes_out)

    assert raw[0] == 1, "one signature slot"
    assert raw[1:65] == SIGNATURE_MARKER
    assert raw[1:65] != bytes(64), "the marker must be distinguishable from unsigned"


def test_the_signed_bytes_still_carry_the_message_that_was_verified() -> None:
    """The seam re-binds AFTER the backend answers, and this is why the marker is a fair
    stand-in: a signature lives outside the message, so filling the slot changes the wire
    bytes and not the binding. A backend that returned a different MESSAGE would be
    refused after being called, and the caller would get nothing."""
    from gecko.txbind import message_binding

    result = _offline(_spec("e"))
    assert result.bytes_out is not None

    rebound = message_binding(result.bytes_out, strength="exact")
    assert rebound[:16] in result.reason


# ---------------------------------------------------------------- the whole-run invariants


def test_every_refusal_yields_no_bytes_and_never_reaches_the_signer() -> None:
    """The one invariant the entire demo exists to demonstrate, asserted over all five."""
    results = run_scenarios(recorded_leg)

    refusals = [r for r in results if r.refused]
    assert len(refusals) == 4, "four of the five scenarios must refuse"
    for result in refusals:
        assert result.bytes_out is None, f"({result.key}) leaked bytes on a refusal"
        assert result.signer_calls == 0, f"({result.key}) reached the signing backend"
    assert [r.key for r in results if not r.refused] == ["e"]


def test_the_five_scenarios_are_distinct_controls_not_five_spellings_of_one() -> None:
    """A demo of five scenarios that all fire the same check proves one thing five times."""
    results = run_scenarios(recorded_leg)

    reasons = [r.reason for r in results]
    assert len(set(reasons)) == 5, f"scenarios collapsed onto one answer: {reasons}"
    controls = {r.control for r in results if r.refused}
    assert len(controls) == 3, (
        f"refusals came from too few distinct controls: {controls}"
    )


def test_b_and_d_share_a_signer_CODE_and_that_is_a_finding_not_a_collapse() -> None:
    """A real observation, written down rather than smoothed over.

    (b) and (d) both surface as ``handoff-not-approved``, because the signer's refusal
    vocabulary describes the HANDOFF it was given — "this one was not approved upstream" —
    and not why ``verify_handoff`` declined. ``SigningVerdict`` carries a free-text
    ``reason`` and no code, so there is nothing structured to propagate; inventing a code
    in the demo would be the demo asserting a distinction the engine does not make.

    So the two are proved distinct HERE, on the sentences the engine actually produced:
    a network refusal and a binding refusal are different answers, and if they ever render
    identically this test says so instead of the demo quietly showing one case twice.
    """
    results = {r.key: r for r in run_scenarios(recorded_leg)}

    assert results["b"].code == results["d"].code == "handoff-not-approved"
    assert results["b"].reason != results["d"].reason
    assert "network" not in results["d"].reason
    assert "receipt attests" not in results["b"].reason


def test_the_recorded_leg_contacts_no_node_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATTERN B, PROVED RATHER THAN CLAIMED. The HTTP transport is broken for the duration
    of this test, so any scenario that reached a real node — including one that quietly
    fell back to a default ``rpc_call`` — raises instead of passing."""

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the recorded leg reached the network")

    monkeypatch.setattr("gecko.rpc._http_post_json", forbidden)
    results = run_scenarios(recorded_leg)

    assert len(results) == 5
    assert sum(1 for r in results if r.refused) == 4


def test_a_build_failure_comes_back_as_a_refusal_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C8, at the level a caller feels it. ``prepare_handoff`` promised "never raises for a
    bad build" while calling ``simulate`` unwrapped — and the ``except Exception`` a caller
    writes to survive that ALSO swallows ``SigningRefused``, the quarantine gate. So the
    fix is not "the demo catches it": the fix is that there is nothing here to catch."""
    from gecko.simulate import SimulateError

    def exploding(*_args: Any, **_kwargs: Any) -> Any:
        raise SimulateError("the builder returned no transaction")

    monkeypatch.setattr("examples.autonomous_signing_demo.build_transfer", exploding)
    result = _offline(_spec("e"))

    assert result.refused is True
    assert result.bytes_out is None
    assert result.signer_calls == 0
    assert result.code == "simulation-withheld-the-bytes"


# ------------------------------------------------------------------- the printed output


def _printed() -> str:
    return render(run_scenarios(recorded_leg), recorded_leg(0).banner)


def test_the_output_says_the_fork_is_not_mainnet() -> None:
    """C13 — the distinction must be in the PRINTED output, not only in a docstring
    nobody reading the terminal will open."""
    assert "THE FORK IS NOT MAINNET" in _printed()


def test_the_output_states_the_single_rpc_trust_root_limit() -> None:
    """C10. §8 item 11 is unresolved and the run must say so: every verification flowed
    through one unauthenticated node, and a hostile node returns a clean simulation for a
    transaction that will not behave as attested."""
    printed = _printed()

    assert "ONE unauthenticated node" in printed
    assert "will not behave as attested" in printed


def test_the_output_calls_the_velocity_counter_ADVISORY() -> None:
    """C6. The counter's storage owner is unsettled — a budget the agent's own process can
    write is a budget the agent resets on itself — so it may be printed only as advisory."""
    printed = _printed()

    assert "ADVISORY, not a control" in printed
    assert "guarantee" not in printed.lower()


def test_the_output_names_the_residuals_this_run_does_not_close() -> None:
    """C1, in the honest form: the control closes OMISSION, not FABRICATION. A demo that
    printed only its wins would be the half-closed control described as closed.

    B2 moved WHICH object that sentence is about — the Receipt's ``origin``, not the
    handoff's shape, because ``sign`` re-verifies the handoff itself and reads every fact
    from the Receipt. The half it closes grew; the phrase this asserts did not, and must
    not: a demo that stopped saying FABRICATION would be claiming the whole thing."""
    printed = _printed()

    assert "OMISSION, not FABRICATION" in printed
    assert "structurally cannot" not in printed
    assert "sign() takes no SpendVerdict" in printed


def test_every_refusal_line_names_what_was_refused_and_that_no_bytes_left() -> None:
    printed = _printed()

    assert printed.count("bytes out: NONE — the refusal yielded no transaction") == 4
    assert "bytes escaping a refusal: NONE" in printed
    for key in ("(a)", "(b)", "(c)", "(d)", "(e)"):
        assert key in printed


# ----------------------------------------------------------------- never mainnet, ever


def test_no_leg_ever_asserts_the_mainnet_network() -> None:
    """The asserted network is the one input the signing gate reads. Both legs assert
    ``fork``; ``mainnet`` appears only as scenario (b)'s EXPECTATION, which exists to be
    refused."""
    assert recorded_leg(0).network == "fork"
    assert [s.expects_network for s in scenarios() if s.expects_network != "fork"] == [
        "mainnet"
    ]
    assert _spec("b").expects_network == "mainnet"


def test_no_new_file_signs_or_broadcasts_on_any_network() -> None:
    """C13, checked as text over both new files.

    The forbidden tokens are assembled from fragments so this test does not trip its own
    check. The alternative — excluding the test file from the scan — is where a real one
    would then be free to appear.
    """
    import pathlib

    forbidden = (
        "send" + "Transaction",
        "Key" + "pair",
        "secret" + "_key",
        "mne" + "monic",
    )
    root = pathlib.Path(__file__).resolve().parents[1]
    for relative in (
        "examples/autonomous_signing_demo.py",
        "tests/test_autonomous_signing_demo.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{relative} mentions {token!r}"


# ------------------------------------------------------------------------- the fork leg


def test_the_fork_leg_refuses_to_pass_quietly_when_surfpool_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE FAILURE THIS PROJECT HAS ALREADY SHIPPED, from the other end. A release once
    passed only because surfpool happened to be running; the mirror image is a ``--fork``
    run that silently degrades to no fork at all and prints a clean bill of health.

    So absence is LOUD and it is non-zero: the exit code, not just the text, refuses.
    """
    monkeypatch.setattr("shutil.which", lambda _binary: None)
    code = main(["--fork"])
    printed = capsys.readouterr().out

    assert code == 2, "an asked-for fork leg that did not run must not exit 0"
    assert "FORK LEG DID NOT RUN" in printed
    assert "not on PATH" in printed
    assert "Nothing above was proved against a validator" in printed


def test_surfpool_status_reports_absence_as_a_sentence_not_a_falsy_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: None)
    status = surfpool_status()

    assert status.available is False
    assert status.path is None
    assert "NOT run" in status.detail and "NOTHING here was proved" in status.detail


@pytest.mark.skipif(
    not surfpool_status().available or os.getenv("GECKO_FORK_DEMO") == "0",
    reason=(
        "surfpool is not on PATH (or GECKO_FORK_DEMO=0) — THE FORK LEG DID NOT RUN and "
        "nothing below was proved against a validator: " + surfpool_status().detail
    ),
)
def test_the_same_five_scenarios_hold_against_a_real_surfpool_mainnet_fork() -> None:
    """The final rung: identical code, real validator, real mainnet-derived state.

    The fork is a LOCAL chain — its payer is funded by a faucet mainnet does not have and
    its blockhash is a surfnet sentinel. Nothing is signed with a real key and nothing is
    broadcast. What the fork adds over the recorded leg is a clock and a real runtime: it
    is what caught the demo reading ``getSlot`` BEFORE the simulation, which made every
    receipt look like it came from the future and drew a correct
    ``receipt-slot-implausible`` refusal from the seam.
    """
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    try:
        with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=90) as fork:
            leg = fork_leg(fork)
            results = [run_attempt(leg, spec) for spec in scenarios()]
    except SurfpoolError as exc:
        pytest.skip(f"THE FORK LEG DID NOT RUN — surfpool never became ready: {exc}")

    assert leg.network == "fork", "a fork leg may never assert mainnet"
    for result in results:
        assert result.leg == "fork"
        if result.refused:
            assert result.bytes_out is None, f"({result.key}) leaked bytes on the fork"
            assert result.signer_calls == 0

    by_key = {r.key: r for r in results}
    assert by_key["a"].code == "over-per-transaction-cap"
    assert by_key["c"].builder_calls == 0
    assert by_key["d"].refused and by_key["d"].policy_authorized is True
    assert by_key["e"].refused is False and by_key["e"].bytes_out is not None
    assert by_key["e"].signer_calls == 1

    print("\n" + render(results, leg.banner))


def test_the_fork_leg_type_carries_a_banner_that_disclaims_mainnet() -> None:
    """Checked on the TYPE rather than only on a live run, so the disclaimer cannot go
    missing on a machine where the fork leg is skipped."""
    leg = DemoLeg(
        name="fork",
        rpc_url="http://127.0.0.1:1",
        rpc_call=lambda _u, _m, _p: {},
        network="fork",
        blockhash="SURFNETxSAFEHASHxxxxxxxxxxxxxxxxxxxxxxxxxx1",
        observe_slot=lambda: 1,
        banner=("x",),
    )
    assert leg.network == "fork"
    assert PAYER not in leg.rpc_url
