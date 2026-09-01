"""submit_transaction — verify -> send -> rebroadcast -> confirm, offline.

Driven by the friction report: an agent with no shell could sign and then STOP,
and the agent under the clock dropped verification. The tool bundles both, and
the binding requirement doubles as relay-abuse prevention: nothing broadcasts
unless it verifies against a Gecko receipt's binding at exact strength."""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("solders")

from gecko.landing import assemble_unsigned_tx  # noqa: E402
from gecko.submit_transaction import submit_transaction_result  # noqa: E402
from gecko.txbind import message_binding  # noqa: E402

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx_with_binding() -> tuple[str, str]:
    """A real unsigned memo tx with a stamped (unverifiable) signature — what a
    signer hands back — plus the exact binding a receipt would have issued."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(Pubkey.from_string(USDC), False, False)
    unsigned = assemble_unsigned_tx([Instruction(program, b"gecko", [meta])], PAYER).tx
    raw = bytearray(base64.b64decode(unsigned))
    raw[1:65] = bytes(
        range(64)
    )  # non-empty signature slot; binding is over the message
    signed = base64.b64encode(bytes(raw)).decode()
    return signed, message_binding(signed, strength="exact")


def test_refuses_without_binding() -> None:
    out = submit_transaction_result(
        {"transaction": "AAAA", "last_valid_block_height": 1}
    )
    assert out["refused"] and out["code"] == "binding-required"


def test_refuses_without_expiry() -> None:
    tx, binding = _tx_with_binding()
    out = submit_transaction_result({"transaction": tx, "binding": binding})
    assert out["refused"] and out["code"] == "expiry-required"


def test_refuses_private_rpc() -> None:
    tx, binding = _tx_with_binding()
    out = submit_transaction_result(
        {
            "transaction": tx,
            "binding": binding,
            "last_valid_block_height": 1,
            "rpc_url": "http://192.168.1.10:8899",
        }
    )
    assert out["refused"] and out["code"] == "rpc-url-refused"


def test_refuses_a_binding_mismatch() -> None:
    tx, _ = _tx_with_binding()
    out = submit_transaction_result(
        {"transaction": tx, "binding": "0" * 64, "last_valid_block_height": 1}
    )
    assert out["refused"] and out["code"] == "verify-failed"


def _fake_rpc(confirm_after: int, height: int = 10):
    calls = {"status_polls": 0, "sends": 0}

    def call(url, method, params):
        if method == "sendTransaction":
            calls["sends"] += 1
            return {"result": "SigFake1111"}
        if method == "getSignatureStatuses":
            calls["status_polls"] += 1
            if calls["status_polls"] >= confirm_after:
                return {
                    "result": {
                        "value": [
                            {"confirmationStatus": "confirmed", "slot": 42, "err": None}
                        ]
                    }
                }
            return {"result": {"value": [None]}}
        if method == "getBlockHeight":
            return {"result": height}
        raise AssertionError(method)

    return call, calls


def test_confirms_after_rebroadcast() -> None:
    tx, binding = _tx_with_binding()
    call, calls = _fake_rpc(confirm_after=3)
    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda _s: None,
    )
    assert out == {
        "refused": False,
        "confirmed": True,
        "signature": "SigFake1111",
        "slot": 42,
        "err": None,
        "confirmation_status": "confirmed",
    }
    # the loop actually rebroadcast (first send + at least the retries before confirm)
    assert calls["sends"] >= 3


def test_expiry_is_honest_and_names_the_next_step() -> None:
    tx, binding = _tx_with_binding()
    call, _ = _fake_rpc(confirm_after=999, height=200)
    out = submit_transaction_result(
        {"transaction": tx, "binding": binding, "last_valid_block_height": 100},
        rpc_call=call,
        sleep=lambda _s: None,
    )
    assert out["expired"] is True and out["spent"] is False
    assert "never re-submit" in out["reason"]
