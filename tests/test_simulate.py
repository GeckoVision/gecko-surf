"""Task 2: the Receipt engine (gecko/simulate.py), offline-falsifiable core.

Every path is proven with an injected ``build_call`` + ``rpc_call`` — no network. A
Receipt asserts "lands vs a snapshot", not price; ``revert_class`` is a categorical
string (the future corpus vocabulary), never a fabricated number.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from gecko.networks import UNKNOWN_NETWORK, Network
from gecko.simulate import (
    BuiltTx,
    Receipt,
    SimulateError,
    classify_revert,
    simulate,
)

TRACKED = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
PLAN = {
    "accounts": {"user": TRACKED},
    "args": {"amount": 1},
    "feePayer": TRACKED,
    "build_url": "https://api.orquestra.dev/api/x/instructions/buy/build",
}


def _build_ok(_plan: Any) -> BuiltTx:
    return BuiltTx(tx="QkFTRTY0VFg=", encoding="base64")  # any canned tx


def _sim_rpc(
    value: dict[str, Any],
    pre_lamports: int | None = None,
    capture: dict[str, Any] | None = None,
):
    """A fake rpc_call: getAccountInfo returns the pre-lamports snapshot,
    simulateTransaction returns the given value. ``capture`` records the sim config."""

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            if pre_lamports is None:
                return {"result": {"value": None}}
            return {"result": {"value": {"lamports": pre_lamports}}}
        if method == "simulateTransaction":
            if capture is not None:
                capture["tx"] = params[0]
                capture["config"] = params[1]
            return {"result": {"value": value}}
        raise AssertionError(f"unexpected method {method}")

    return rpc


# --- classify_revert: the categorical vocabulary -----------------------------


def test_classify_none_when_no_err() -> None:
    assert classify_revert(None, []) is None


def test_classify_custom_program_error() -> None:
    err = {"InstructionError": [5, {"Custom": 6002}]}
    assert (
        classify_revert(err, ["Program log: something"]) == "custom_program_error:6002"
    )


def test_classify_slippage_overrides_custom() -> None:
    err = {"InstructionError": [5, {"Custom": 6002}]}
    logs = ["Program log: Error: TooMuchSolRequired"]
    assert classify_revert(err, logs) == "slippage"


def test_classify_insufficient_funds() -> None:
    err = {"InstructionError": [0, "SomeError"]}
    logs = ["Transfer: insufficient lamports"]
    assert classify_revert(err, logs) == "insufficient_funds"


def test_classify_account_error() -> None:
    err = {"InstructionError": [2, "AccountOwnedByWrongProgram"]}
    logs = ["Program log: AccountOwnedByWrongProgram"]
    assert classify_revert(err, logs) == "account_error"


def test_classify_account_not_initialized_beats_custom_code() -> None:
    # AnchorError 3012 (associated_user AccountNotInitialized) — the log name is more
    # actionable than the raw code, so it classifies as account_error, not custom_program_error.
    err = {"InstructionError": [0, {"Custom": 3012}]}
    logs = [
        "Program log: AnchorError caused by account: associated_user. "
        "Error Code: AccountNotInitialized. Error Number: 3012."
    ]
    assert classify_revert(err, logs) == "account_error"


def test_classify_other() -> None:
    assert classify_revert("BlockhashNotFound", []) == "other"


# --- simulate(): the Receipt ------------------------------------------------


def test_simulate_pass_reports_units_and_sol_delta() -> None:
    value = {
        "err": None,
        "unitsConsumed": 31000,
        "logs": ["Program log: buy", "Program success"],
        "accounts": [{"lamports": 900}],
    }
    receipt = simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value, pre_lamports=1000),
        build_call=_build_ok,
        track=[TRACKED],
        network=UNKNOWN_NETWORK,
    )
    assert isinstance(receipt, Receipt)
    assert receipt.status == "pass"
    assert receipt.err is None
    assert receipt.revert_class is None
    assert receipt.units_consumed == 31000
    assert receipt.sol_delta == -100  # 900 - 1000
    assert receipt.logs_tail[-1] == "Program success"
    assert "not mainnet" in receipt.network_label


def test_simulate_fail_slippage() -> None:
    value = {
        "err": {"InstructionError": [5, {"Custom": 6002}]},
        "unitsConsumed": 12000,
        "logs": ["Program log: Error: TooMuchSolRequired"],
    }
    receipt = simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    assert receipt.status == "fail"
    assert receipt.revert_class == "slippage"


def test_simulate_fail_custom() -> None:
    value = {
        "err": {"InstructionError": [5, {"Custom": 6002}]},
        "logs": ["Program log: ordinary error"],
    }
    receipt = simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    assert receipt.status == "fail"
    assert receipt.revert_class == "custom_program_error:6002"


def test_simulate_fail_account_error() -> None:
    value = {
        "err": {"InstructionError": [2, "AccountOwnedByWrongProgram"]},
        "logs": ["Program log: AccountOwnedByWrongProgram"],
    }
    receipt = simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )
    assert receipt.status == "fail"
    assert receipt.revert_class == "account_error"


def test_network_label_propagates() -> None:
    value = {"err": None, "unitsConsumed": 1, "logs": []}
    receipt = simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value),
        build_call=_build_ok,
        network_label="surfpool fork (mainnet-backed — NOT mainnet)",
        network=UNKNOWN_NETWORK,
    )
    assert receipt.network_label == "surfpool fork (mainnet-backed — NOT mainnet)"


def test_simulate_passes_the_built_tx_encoding_through() -> None:
    # Orquestra returns the tx in base58; the sim config must carry THAT encoding, not a
    # hardcoded base64 (passing the wrong encoding is a silent decode failure).
    captured: dict[str, Any] = {}
    value = {"err": None, "unitsConsumed": 1, "logs": []}
    simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value, capture=captured),
        build_call=lambda _p: BuiltTx(tx="3base58tx", encoding="base58"),
        network=UNKNOWN_NETWORK,
    )
    assert captured["tx"] == "3base58tx"
    assert captured["config"]["encoding"] == "base58"


def test_default_build_call_prefers_serialized_and_carries_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Orquestra returns BOTH `transaction` (oversized/unusable) and `serializedTransaction`
    # (the real signable tx) in base58. The default builder must pick serializedTransaction
    # and report the response's encoding.
    def fake_post(url: str, body: bytes) -> dict[str, Any]:
        return {
            "transaction": "X" * 5000,  # oversized decoy
            "serializedTransaction": "3realsignabletx",
            "encoding": "base58",
        }

    monkeypatch.setattr("gecko.simulate._http_post_json", fake_post)
    captured: dict[str, Any] = {}
    value = {"err": None, "unitsConsumed": 1, "logs": []}
    simulate(
        PLAN,
        rpc_url="http://127.0.0.1:8899",
        rpc_call=_sim_rpc(value, capture=captured),
        network=UNKNOWN_NETWORK,
        # no build_call → exercises _default_build_call
    )
    assert captured["tx"] == "3realsignabletx"
    assert captured["config"]["encoding"] == "base58"


def test_default_build_call_raises_when_no_tx_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # inject the HTTP seam gecko.simulate uses to build the tx; response lacks any tx key
    def fake_post(url: str, body: bytes) -> dict[str, Any]:
        return {"status": "ok", "no_tx_here": True}

    monkeypatch.setattr("gecko.simulate._http_post_json", fake_post)
    value = {"err": None, "unitsConsumed": 1, "logs": []}
    with pytest.raises(SimulateError):
        simulate(
            PLAN,
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_sim_rpc(value),
            network=UNKNOWN_NETWORK,
            # no build_call → uses the default which POSTs build_url
        )


def test_default_build_call_wraps_http_error_as_simulate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a 403 from the builder (auth/bad payload) is a build-transport failure, not a
    # program revert — it must become a typed SimulateError carrying the status but no body
    def fake_post(url: str, body: bytes) -> dict[str, Any]:
        raise urllib.error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr("gecko.simulate._http_post_json", fake_post)
    value = {"err": None, "unitsConsumed": 1, "logs": []}
    with pytest.raises(SimulateError) as exc:
        simulate(
            PLAN,
            rpc_url="http://127.0.0.1:8899",
            rpc_call=_sim_rpc(value),
            network=UNKNOWN_NETWORK,
        )
    assert "403" in str(exc.value)


# --- D6: the slot the snapshot was taken at (RECORDED, never enforced) --------


def _slot_rpc(result: dict[str, Any]):
    """A fake rpc_call that returns a whole ``result`` object — context included."""

    def rpc(_url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        if method == "simulateTransaction":
            return {"result": result}
        return {"result": {"value": None}}

    return rpc


def _receipt_for(
    result: dict[str, Any], *, network: Network = UNKNOWN_NETWORK
) -> Receipt:
    return simulate(
        PLAN,
        rpc_url="https://api.example.com",
        rpc_call=_slot_rpc(result),
        build_call=_build_ok,
        network=network,
    )


def test_the_context_slot_is_carried_onto_the_receipt() -> None:
    """``simulate`` read ``result.value`` and threw ``result.context`` away, so nothing
    on a Receipt said WHEN the snapshot was — which made a receipt-age bound
    inexpressible, not merely unenforced."""
    receipt = _receipt_for(
        {"context": {"slot": 318_492_001}, "value": {"err": None, "logs": []}}
    )

    assert receipt.observed_slot == 318_492_001


@pytest.mark.parametrize(
    "context",
    [
        None,
        {},
        {"slot": "318492001"},  # a string is not coerced — two spellings, one receipt
        {"slot": 318_492_001.0},  # a float is not truncated
        {"slot": True},  # isinstance(True, int) is True in Python
        {"slot": -1},
        {"slot": 0},  # the name of this test promised this case; it was never here
        "not-a-mapping",
    ],
)
def test_an_unusable_slot_is_absent_never_zero_and_never_coerced(context: Any) -> None:
    """The slot arrives over untrusted transport, so it is TYPE-CHECKED rather than
    trusted. Absence is ``None``: zero is a CLAIM ("slot zero"), and a false one."""
    result: dict[str, Any] = {"value": {"err": None, "logs": []}}
    if context is not None:
        result["context"] = context

    assert _receipt_for(result).observed_slot is None


def test_the_slot_is_enforced_in_exactly_one_place_and_the_gate_is_not_it() -> None:
    """REPLACES ``test_the_slot_is_recorded_and_nothing_enforces_it``, whose headline went
    stale the day :mod:`gecko.signer` shipped an age bound.

    D6 delivered the ability to EXPRESS a freshness bound and deliberately enforced
    nothing. That is no longer the state of the world, and a test asserting "nothing
    enforces it" would now be a claim about a control's ABSENCE while the control exists —
    the same defect as claiming one that does not. So the assertion moves from "nobody
    reads it" to "exactly one place reads it, and it is not the gate":

    * ``txbind.py`` — the signing gate — still never mentions the slot. It answers a
      question about IDENTITY (are these the bytes the receipt attests); age is a
      different question, and folding one into the other would make a stale-but-matching
      transaction indistinguishable from a fresh-but-substituted one.
    * ``handoff.py`` mentions it only to WRITE ``None`` onto the receipt for a run that
      never happened. It performs no read — asserted over the AST rather than the text, so
      a future ``receipt.observed_slot`` comparison cannot slip in under this name.
    * ``signer.py`` reads it, and is the only module that does: the age bound is the
      SECOND of two independent freshness reasons, beside ``require="exact"``.
    """
    import ast
    from pathlib import Path

    import gecko.txbind as txbind_module

    package = Path(txbind_module.__file__).parent

    assert "observed_slot" not in (package / "txbind.py").read_text()

    def reads_the_slot(module: str) -> bool:
        tree = ast.parse((package / module).read_text())
        return any(
            isinstance(node, ast.Attribute) and node.attr == "observed_slot"
            for node in ast.walk(tree)
        )

    assert not reads_the_slot("handoff.py"), (
        "handoff.py must not READ the slot — it only writes None for an unobserved run"
    )
    assert reads_the_slot("signer.py"), (
        "the age bound lost its only reader; a receipt would stop expiring again"
    )


# --- D2: the network the snapshot was taken ON -------------------------------


def test_the_catch_all_is_a_thing_a_caller_may_say_and_it_claims_nothing() -> None:
    """``simulate`` has NO default for ``network`` — a caller who cannot honestly name
    one says ``unknown`` out loud, and that assertion rides onto the Receipt unchanged.
    A silence and a stated 'I do not know' look identical on the Receipt on purpose;
    what differs is that one of them was a decision somebody made."""
    receipt = _receipt_for({"value": {"err": None, "logs": []}})

    assert receipt.network == UNKNOWN_NETWORK


def test_an_asserted_network_rides_onto_the_receipt() -> None:
    receipt = simulate(
        PLAN,
        rpc_url="https://api.example.com",
        rpc_call=_slot_rpc({"value": {"err": None, "logs": []}}),
        build_call=_build_ok,
        network="devnet",
    )

    assert receipt.network == "devnet"


def test_the_network_is_never_read_off_the_rpc_url() -> None:
    """A fork proxy answers at any hostname, so the URL is attacker-influenceable and
    is evidence of nothing. Pointing at the most mainnet-looking URL there is still
    yields a receipt that asserts nothing."""
    receipt = simulate(
        PLAN,
        rpc_url="https://api.mainnet-beta.solana.com",
        rpc_call=_slot_rpc({"value": {"err": None, "logs": []}}),
        build_call=_build_ok,
        network=UNKNOWN_NETWORK,
    )

    assert receipt.network == UNKNOWN_NETWORK
