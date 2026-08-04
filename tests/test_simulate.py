"""Task 2: the Receipt engine (gecko/simulate.py), offline-falsifiable core.

Every path is proven with an injected ``build_call`` + ``rpc_call`` — no network. A
Receipt asserts "lands vs a snapshot", not price; ``revert_class`` is a categorical
string (the future corpus vocabulary), never a fabricated number.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from gecko.simulate import (
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


def _build_ok(_plan: Any) -> str:
    return "QkFTRTY0VFg="  # any canned base64 tx


def _sim_rpc(value: dict[str, Any], pre_lamports: int | None = None):
    """A fake rpc_call: getAccountInfo returns the pre-lamports snapshot,
    simulateTransaction returns the given value."""

    def rpc(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if method == "getAccountInfo":
            if pre_lamports is None:
                return {"result": {"value": None}}
            return {"result": {"value": {"lamports": pre_lamports}}}
        if method == "simulateTransaction":
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
    )
    assert receipt.network_label == "surfpool fork (mainnet-backed — NOT mainnet)"


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
        simulate(PLAN, rpc_url="http://127.0.0.1:8899", rpc_call=_sim_rpc(value))
    assert "403" in str(exc.value)
