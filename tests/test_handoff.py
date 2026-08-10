"""``prepare_handoff`` / ``verify_handoff`` — the last mile between a Receipt and a signer.

Offline (Pattern B): the builder and the RPC are injected, so every behaviour below is
falsifiable with no network and no key.

WHY THIS MODULE EXISTS, AND WHY IT IS NOW TWO FUNCTIONS. ``txbind.evaluate_tx`` — the
gate that decides whether the transaction in front of a signer is the one a Receipt
attests — shipped with **zero callers outside its own module**. The first fix gave it
one caller, ``prepare_handoff``, which built a transaction, simulated it, and then
"verified" the very object it had just captured. Both sides of the comparison came from
one variable, so the inequality could not fire: the refusal branch was unreachable and
the approval said only "these bytes equal themselves".

So the two jobs are now two functions, because they answer different questions:

* ``prepare_handoff(plan, ...)`` — build + simulate. Returns the bytes WE simulated and
  the Receipt for them. It makes **no verification claim** and has no ``approved`` field.
* ``verify_handoff(tx, receipt, require=...)`` — takes bytes from SOMEWHERE ELSE and
  decides whether the Receipt attests them. Subject bytes are a required positional, so
  there is no path to ``approved=True`` over bytes this module produced itself.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from gecko.handoff import prepare_handoff, verify_handoff
from gecko.landing import assemble_unsigned_tx
from gecko.simulate import BuiltTx
from gecko.txbind import message_binding

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PLAN = {"build_url": "https://builder.example.com/build", "accounts": {}, "args": {}}
#: An arbitrary well-formed blockhash — base58 of 32 non-zero bytes.
OTHER_BLOCKHASH = "2b8kBmnjHwXTvHzX1Y3PrRTVFwRRQRzGgLBGrfHtmH5J"


def _memo_tx(payload: bytes, *, blockhash: str | None = None) -> str:
    """A minimal real unsigned transaction, base64 — one memo instruction."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(Pubkey.from_string(USDC), False, False)
    return assemble_unsigned_tx(
        [Instruction(program, payload, [meta])], PAYER, blockhash=blockhash
    ).tx


def _builder(tx: str, encoding: str = "base64"):
    def build(_plan: Any) -> BuiltTx:
        return BuiltTx(tx=tx, encoding=encoding)

    return build


def _rpc(err: Any = None, units: int = 4_200, **_ignored: Any):
    def call(_url: str, method: str, _params: Any) -> dict[str, Any]:
        if method == "simulateTransaction":
            return {
                "result": {
                    "value": {
                        "err": err,
                        "logs": ["Program log: ok"],
                        "unitsConsumed": units,
                    }
                }
            }
        return {"result": {"value": None}}

    return call


def _prepared(
    tx: str, encoding: str = "base64", *, network: str = "mainnet", **kwargs: Any
):
    return prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder(tx, encoding),
        rpc_call=_rpc(**kwargs),
        network=network,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- prepare


def test_prepare_returns_the_bytes_it_simulated_and_claims_nothing_more() -> None:
    """REPLACES a test that asserted a tautology. The old happy path recomputed the
    binding from the payload and compared it to the Receipt's — but both came from the
    same captured object, so it could only ever pass. It read as "the bytes are verified"
    while proving that a value equals itself.

    What ``prepare_handoff`` may honestly claim is narrower: the simulation passed, and
    these are the bytes that were simulated. It exposes no ``approved`` field, because it
    verifies nothing — verification needs bytes from somewhere else."""
    prepared = _prepared(_memo_tx(b"buy water"))

    assert prepared.simulation_passed is True
    assert prepared.simulated_transaction_base64 is not None
    assert prepared.units_consumed == 4_200
    assert not hasattr(prepared, "approved"), (
        "prepare_handoff must not expose an approval — it verifies nothing"
    )
    assert not hasattr(prepared, "transaction_base64"), (
        "the payload field must say the bytes are merely SIMULATED, not verified"
    )
    # Internal consistency ONLY, and labelled as such: the payload is the same object the
    # Receipt was bound to. This is not evidence of verification, it is evidence that
    # `prepare` did not re-encode the wrong buffer on the way out.
    assert prepared.receipt.message_binding == message_binding(
        prepared.simulated_transaction_base64, encoding="base64", strength="structural"
    )


def test_prepare_carries_the_receipt_so_a_verifier_can_be_handed_one() -> None:
    """The Receipt has to leave this function, or ``verify_handoff`` has nothing to check
    against and the caller re-simulates (a second builder POST, a fresh blockhash, and a
    different transaction) to get one."""
    prepared = _prepared(_memo_tx(b"buy water"))

    assert prepared.receipt.status == "pass"
    assert prepared.receipt.binding_strength == "structural"
    assert prepared.binding == prepared.receipt.message_binding


def test_the_builder_is_called_exactly_once() -> None:
    """GUARD. The first draft of this module called the builder a second time to recover
    the bytes `simulate` does not return. That is a second POST to the builder, and
    Orquestra's `/build` embeds a FRESH `recentBlockhash` on every call — so the
    transaction being returned would not be the transaction that was simulated. A
    structural binding hides the difference (it normalises the blockhash out) and an exact
    one refuses a perfectly good plan. Both are wrong; the bytes must be captured."""
    calls: list[int] = []
    tx = _memo_tx(b"buy water")

    def counting_build(_plan: Any) -> BuiltTx:
        calls.append(1)
        return BuiltTx(tx=tx, encoding="base64")

    prepared = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=counting_build,
        rpc_call=_rpc(),
    )

    assert prepared.simulation_passed is True
    assert len(calls) == 1, f"builder called {len(calls)}x — it must be called once"


def test_a_reverting_plan_hands_over_nothing() -> None:
    """A failed simulation must not carry a payload. If the bytes are still in the return
    value, the next caller signs them and the simulation was decoration."""
    prepared = _prepared(
        _memo_tx(b"revert"), err={"InstructionError": [0, {"Custom": 3012}]}
    )

    assert prepared.simulation_passed is False
    assert prepared.simulated_transaction_base64 is None
    assert "did not pass" in prepared.reason


def test_a_base58_builder_still_yields_base64_for_the_signer() -> None:
    """Orquestra's ``/build`` returns base58; ``@orquestradev/signer-mcp`` decodes base64
    (``Buffer.from(tx, 'base64')``). Handing the signer base58 is a decode failure at the
    far end of a live demo, so the conversion belongs here, once."""
    raw = base64.b64decode(_memo_tx(b"buy water"))
    from gecko.txbind import _B58

    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, rem = divmod(number, 58)
        digits = _B58[rem] + digits
    b58 = "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + digits

    prepared = _prepared(b58, "base58")

    assert prepared.simulation_passed is True
    assert prepared.simulated_transaction_base64 is not None
    assert base64.b64decode(prepared.simulated_transaction_base64, validate=True) == raw


def test_an_undecodable_build_hands_over_nothing() -> None:
    """A builder that returns something we cannot decode yields no binding, and a Receipt
    with no binding attests no specific transaction. Handing those bytes on would put
    unverifiable material in front of a signer, so it must read as NO."""
    prepared = _prepared("not-a-transaction")

    assert prepared.simulation_passed is False
    assert prepared.simulated_transaction_base64 is None
    assert "binding" in prepared.reason


def test_the_handoff_carries_no_key_no_signature_and_no_send() -> None:
    """Structural guarantee, asserted rather than promised in prose: the signature slots
    in what we hand over are still zero. Gecko produces an UNSIGNED transaction; signing
    and sending belong to whoever holds the key."""
    prepared = _prepared(_memo_tx(b"buy water"))
    assert prepared.simulated_transaction_base64 is not None
    raw = base64.b64decode(prepared.simulated_transaction_base64)
    count = raw[0]
    assert count >= 1
    assert raw[1 : 1 + 64 * count] == bytes(64 * count)


# ---------------------------------------------------------------------------- verify


def test_verify_refuses_a_transaction_the_receipt_does_not_attest() -> None:
    """THE TEST THE OLD HAPPY PATH ONLY LOOKED LIKE. Simulate one plan, then present a
    DIFFERENT transaction — the swapped-route case the whole gate exists for. Under the
    previous single-function API this was unreachable: the only bytes that could ever
    reach the comparison were the ones it had just captured."""
    prepared = _prepared(_memo_tx(b"buy water"))
    swapped = _memo_tx(b"drain wallet")

    verdict = verify_handoff(swapped, prepared.receipt, require="structural")

    assert verdict.approved is False
    assert verdict.transaction_base64 is None
    assert "NOT the one" in verdict.reason


def test_verify_approves_the_transaction_the_receipt_attests() -> None:
    """The other side of the same comparison, over bytes handed back in from outside."""
    prepared = _prepared(_memo_tx(b"buy water"))
    assert prepared.simulated_transaction_base64 is not None

    verdict = verify_handoff(
        prepared.simulated_transaction_base64, prepared.receipt, require="structural"
    )

    assert verdict.approved is True
    assert verdict.transaction_base64 == prepared.simulated_transaction_base64
    assert verdict.units_consumed == 4_200


def test_verify_returns_the_subject_bytes_never_the_simulated_ones() -> None:
    """The window re-opens from the other side if the verdict returns the bytes we
    simulated after checking DIFFERENT bytes: the caller then signs something that was
    never the subject of the check.

    This fixture is also the RESIDUAL, named on purpose. A structural binding normalises
    the blockhash out, so a subject differing from the simulated transaction ONLY in its
    blockhash MATCHES — and is approved, and is returned. Those exact bytes were never
    simulated. That is the honest ceiling of a `replaceRecentBlockhash:true` simulation
    and it is what D4 (the T6/T7 pair) has to close; until then the rule that keeps it
    survivable is this one: whatever comes back is the subject, byte for byte."""
    simulated = _memo_tx(b"buy water")
    rehashed = _memo_tx(b"buy water", blockhash=OTHER_BLOCKHASH)
    assert rehashed != simulated, "fixture is broken — the two must differ on the wire"

    prepared = _prepared(simulated)
    verdict = verify_handoff(rehashed, prepared.receipt, require="structural")

    assert verdict.approved is True, "structural binding is blockhash-blind by design"
    assert verdict.transaction_base64 == rehashed
    assert verdict.transaction_base64 != prepared.simulated_transaction_base64


def test_verify_takes_base58_subject_bytes_and_returns_base64() -> None:
    """A signer that hands its bytes back in base58 must not be silently refused, and the
    payload it gets back is still the base64 its decoder expects — the SAME bytes,
    re-spelled, never a substitution."""
    simulated = _memo_tx(b"buy water")
    raw = base64.b64decode(simulated)
    from gecko.txbind import _B58

    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, rem = divmod(number, 58)
        digits = _B58[rem] + digits
    b58 = "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + digits

    prepared = _prepared(simulated)
    verdict = verify_handoff(
        b58, prepared.receipt, require="structural", encoding="base58"
    )

    assert verdict.approved is True
    assert verdict.transaction_base64 is not None
    assert base64.b64decode(verdict.transaction_base64, validate=True) == raw


def test_verify_will_not_pick_a_strength_for_the_caller() -> None:
    """``require`` has NO default. A default is a strength nobody chose, and the only
    safe-by-omission value (`exact`) refuses every fork simulation, so the tempting
    default is the weak one. mypy enforces this statically; this asserts it at runtime so
    a later 'convenience' default fails loudly."""
    prepared = _prepared(_memo_tx(b"buy water"))
    assert prepared.simulated_transaction_base64 is not None

    with pytest.raises(TypeError):
        verify_handoff(prepared.simulated_transaction_base64, prepared.receipt)  # type: ignore[call-arg]


def test_verify_requiring_exact_refuses_a_structural_receipt() -> None:
    """Asking for more than the receipt earned is a refusal, never a silent downgrade.
    A fork simulation replaces the blockhash, so it can only ever bind structurally."""
    prepared = _prepared(_memo_tx(b"buy water"))
    assert prepared.simulated_transaction_base64 is not None

    verdict = verify_handoff(
        prepared.simulated_transaction_base64, prepared.receipt, require="exact"
    )

    assert verdict.approved is False
    assert verdict.transaction_base64 is None
    assert verdict.strength == "structural"


def test_verify_refuses_a_receipt_that_did_not_pass() -> None:
    """The bytes may well be the ones simulated — and the simulation said no. A binding
    match over a failing Receipt is not an approval."""
    prepared = _prepared(
        _memo_tx(b"revert"), err={"InstructionError": [0, {"Custom": 3012}]}
    )
    verdict = verify_handoff(
        _memo_tx(b"revert"), prepared.receipt, require="structural"
    )

    assert verdict.approved is False
    assert verdict.transaction_base64 is None
    assert "did not pass" in verdict.reason


def test_verify_refuses_undecodable_subject_bytes_without_raising() -> None:
    """A gate that throws is a gate someone wraps in try/except and bypasses. Garbage in
    is a refusal, with no payload."""
    prepared = _prepared(_memo_tx(b"buy water"))

    verdict = verify_handoff(
        "!!! not a transaction !!!", prepared.receipt, require="structural"
    )

    assert verdict.approved is False
    assert verdict.transaction_base64 is None


# ------------------------------------------------------------------- the network (D2)


def test_prepare_records_the_network_the_simulation_actually_ran_against() -> None:
    """``prepare_handoff`` and ``verify_handoff`` need DIFFERENT network facts and the
    two must not be collapsed: this one records where the simulation ran, and it is the
    only one this function can honestly know."""
    prepared = _prepared(_memo_tx(b"buy water"), network="fork")

    assert prepared.receipt.network == "fork"
    assert prepared.network == "fork"


def test_prepare_will_not_pick_a_network_for_the_caller() -> None:
    """No permissive default: a run whose network nobody stated must not read as one
    that was checked."""
    with pytest.raises(TypeError):
        prepare_handoff(
            PLAN,
            rpc_url="https://rpc.example.com",
            build_call=_builder(_memo_tx(b"buy water")),
            rpc_call=_rpc(),
        )  # type: ignore[call-arg]


def test_verify_refuses_a_fork_receipt_for_a_mainnet_signature() -> None:
    """The D2 fail-open at the handoff seam: same bytes, same binding, and a snapshot of
    a network nobody is committing into."""
    prepared = _prepared(_memo_tx(b"buy water"), network="fork")
    assert prepared.simulated_transaction_base64 is not None

    verdict = verify_handoff(
        prepared.simulated_transaction_base64,
        prepared.receipt,
        require="structural",
        expected_network="mainnet",
    )

    assert verdict.approved is False
    assert verdict.transaction_base64 is None


def test_verify_approves_when_the_caller_expects_the_network_that_was_simulated() -> (
    None
):
    prepared = _prepared(_memo_tx(b"buy water"), network="fork")
    assert prepared.simulated_transaction_base64 is not None

    verdict = verify_handoff(
        prepared.simulated_transaction_base64,
        prepared.receipt,
        require="structural",
        expected_network="fork",
    )

    assert verdict.approved is True
    assert verdict.network == "fork"


def test_verify_will_not_pick_a_network_for_the_caller() -> None:
    prepared = _prepared(_memo_tx(b"buy water"), network="fork")
    assert prepared.simulated_transaction_base64 is not None

    with pytest.raises(TypeError):
        verify_handoff(
            prepared.simulated_transaction_base64,
            prepared.receipt,
            require="structural",
        )  # type: ignore[call-arg]
