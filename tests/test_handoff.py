"""``prepare_handoff`` — the last mile between a Receipt and someone else's signer.

Offline (Pattern B): the builder and the RPC are injected, so every behaviour below is
falsifiable with no network and no key.

WHY THIS MODULE EXISTS. ``txbind.evaluate_tx`` — the gate that decides whether the
transaction in front of a signer is the one a Receipt attests — shipped with **zero
callers outside its own module**. A gate nothing calls is not a gate, and the repo has
now met that shape often enough to name it: when the unsafe answer is the one available
for free, it is the one that ships. The free answer here was "hand the builder's base58
straight to the signer", which skips the verdict entirely.

So the payload and the verdict are produced by ONE function, and the payload is
``None`` whenever the verdict refuses. A caller cannot reach the bytes without passing
the gate, because there is no code path that returns them unapproved.
"""

from __future__ import annotations

import base64
from typing import Any

from gecko.handoff import prepare_handoff
from gecko.landing import assemble_unsigned_tx
from gecko.simulate import BuiltTx
from gecko.txbind import message_binding

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PLAN = {"build_url": "https://builder.example.com/build", "accounts": {}, "args": {}}


def _memo_tx(payload: bytes) -> str:
    """A minimal real unsigned transaction, base64 — one memo instruction."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(Pubkey.from_string(USDC), False, False)
    return assemble_unsigned_tx([Instruction(program, payload, [meta])], PAYER).tx


def _builder(tx: str, encoding: str = "base64"):
    def build(_plan: Any) -> BuiltTx:
        return BuiltTx(tx=tx, encoding=encoding)

    return build


def _rpc(err: Any = None, units: int = 4_200):
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


def test_a_passing_plan_hands_over_bytes_the_receipt_actually_attests() -> None:
    """The happy path, and the property that makes it worth anything: the bytes handed
    to the signer hash to the binding the Receipt carries. Not "a transaction" — THIS one."""
    tx = _memo_tx(b"buy water")
    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder(tx),
        rpc_call=_rpc(),
    )

    assert handoff.approved is True
    assert handoff.transaction_base64 is not None
    assert handoff.units_consumed == 4_200
    # The payload really is what was simulated — recompute the binding from the bytes
    # we are about to hand over rather than trusting the field beside them.
    recomputed = message_binding(
        handoff.transaction_base64, encoding="base64", strength="structural"
    )
    assert recomputed == handoff.binding


def test_the_builder_is_called_exactly_once() -> None:
    """GUARD. The first draft of this module called the builder a second time to recover
    the bytes `simulate` does not return. That is a second POST to the builder, and
    Orquestra's `/build` embeds a FRESH `recentBlockhash` on every call — so the
    transaction being verified would not be the transaction that was simulated. A
    structural binding hides the difference (it normalises the blockhash out) and an exact
    one refuses a perfectly good plan. Both are wrong; the bytes must be captured."""
    calls: list[int] = []
    tx = _memo_tx(b"buy water")

    def counting_build(_plan: Any) -> BuiltTx:
        calls.append(1)
        return BuiltTx(tx=tx, encoding="base64")

    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=counting_build,
        rpc_call=_rpc(),
    )

    assert handoff.approved is True
    assert len(calls) == 1, f"builder called {len(calls)}x — it must be called once"


def test_a_reverting_plan_hands_over_nothing() -> None:
    """A refusal must not carry a payload. If the bytes are still in the return value,
    the next caller uses them and the gate was decoration."""
    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder(_memo_tx(b"revert")),
        rpc_call=_rpc(err={"InstructionError": [0, {"Custom": 3012}]}),
    )

    assert handoff.approved is False
    assert handoff.transaction_base64 is None
    assert "did not pass" in handoff.reason


def test_requiring_an_exact_binding_refuses_a_structural_receipt() -> None:
    """Asking for more than the receipt earned is a refusal, never a silent downgrade.
    A fork simulation replaces the blockhash, so it can only ever bind structurally."""
    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder(_memo_tx(b"buy water")),
        rpc_call=_rpc(),
        require="exact",
    )

    assert handoff.approved is False
    assert handoff.transaction_base64 is None
    assert handoff.strength == "structural"


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

    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder(b58, encoding="base58"),
        rpc_call=_rpc(),
    )

    assert handoff.approved is True
    assert handoff.transaction_base64 is not None
    assert base64.b64decode(handoff.transaction_base64, validate=True) == raw


def test_an_undecodable_build_refuses_instead_of_guessing() -> None:
    """A builder that returns something we cannot decode yields no binding, and a receipt
    with no binding attests no specific transaction. That must read as NO."""
    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder("not-a-transaction", encoding="base64"),
        rpc_call=_rpc(),
    )

    assert handoff.approved is False
    assert handoff.transaction_base64 is None


def test_the_handoff_carries_no_key_no_signature_and_no_send() -> None:
    """Structural guarantee, asserted rather than promised in prose: the signature slots
    in what we hand over are still zero. Gecko produces an UNSIGNED transaction; signing
    and sending belong to whoever holds the key."""
    handoff = prepare_handoff(
        PLAN,
        rpc_url="https://rpc.example.com",
        build_call=_builder(_memo_tx(b"buy water")),
        rpc_call=_rpc(),
    )
    assert handoff.transaction_base64 is not None
    raw = base64.b64decode(handoff.transaction_base64)
    count = raw[0]
    assert count >= 1
    assert raw[1 : 1 + 64 * count] == bytes(64 * count)
