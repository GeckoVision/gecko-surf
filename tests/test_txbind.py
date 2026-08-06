"""``evaluate_tx`` — the receipt→signature binding.

Offline (Pattern B): messages are assembled locally, no RPC, no network. The behaviours
that matter here are the refusals — a signing gate is worth exactly what it refuses, and
every "we could not check" path must render as NO, never as silence.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest

from gecko.landing import assemble_unsigned_tx
from gecko.simulate import Receipt
from gecko.txbind import (
    SigningVerdict,
    TxDecodeError,
    evaluate_tx,
    message_binding,
)

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
OTHER = "FFWtrEQ4B4PKQoVuHYzZq8FabGkVatYzDpEVHsK5rrhF"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _memo(payload: bytes, payer: str = PAYER) -> str:
    """A minimal real transaction: one memo instruction, base64, unsigned."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(
        pubkey=Pubkey.from_string(USDC), is_signer=False, is_writable=False
    )
    return assemble_unsigned_tx([Instruction(program, payload, [meta])], payer).tx


def _receipt(
    binding: str | None, strength: str | None, status: str = "pass"
) -> Receipt:
    return Receipt(
        status=status,  # type: ignore[arg-type]
        err=None,
        revert_class=None if status == "pass" else "account_error",
        units_consumed=50_000,
        sol_delta=None,
        tokens_received=None,
        logs_tail=(),
        network_label="simulated (fork/RPC snapshot — not mainnet)",
        message_binding=binding,
        binding_strength=strength,
    )


# --------------------------------------------------------------------------- #
# The binding itself.
# --------------------------------------------------------------------------- #
def test_the_same_message_binds_to_the_same_hash() -> None:
    tx = _memo(b"water")

    assert message_binding(tx) == message_binding(tx)


def test_a_changed_instruction_changes_the_binding() -> None:
    """The whole point: a rewritten payload must not pass as the approved plan."""
    assert message_binding(_memo(b"water")) != message_binding(_memo(b"whisky"))


def test_a_changed_fee_payer_changes_the_binding() -> None:
    assert message_binding(_memo(b"water")) != message_binding(_memo(b"water", OTHER))


def test_structural_and_exact_never_collide() -> None:
    """A structural digest must never be mistakable for an exact one, or a caller that
    demanded the stronger proof could be handed the weaker one."""
    tx = _memo(b"water")

    assert message_binding(tx, strength="structural") != message_binding(
        tx, strength="exact"
    )


def test_an_undecodable_transaction_raises_without_echoing_it() -> None:
    with pytest.raises(TxDecodeError) as caught:
        message_binding("not-a-transaction-at-all")

    assert "not-a-transaction-at-all" not in str(caught.value)


def test_base58_encoded_json_is_refused_not_hashed() -> None:
    """Observed live on a real builder: the `transaction` field is base58-encoded JSON —
    a DESCRIPTION of a transaction, not a signable one. Copying it from a landing demo
    gets you something that can never be signed, so it must fail closed rather than hash
    into a plausible-looking binding."""
    from gecko.txbind import _B58

    payload = b'{"feePayer":"x","instructions":[]}'
    number = int.from_bytes(payload, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _B58[remainder] + encoded

    with pytest.raises(TxDecodeError, match="neither legacy nor v0"):
        message_binding(encoded, encoding="base58")


# --------------------------------------------------------------------------- #
# The gate.
# --------------------------------------------------------------------------- #
def test_the_attested_transaction_is_approved() -> None:
    tx = _memo(b"water")

    verdict = evaluate_tx(tx, _receipt(message_binding(tx), "structural"))

    assert verdict.approved
    assert "blockhash NOT covered" in verdict.reason  # the caveat always travels


def test_a_different_transaction_is_refused() -> None:
    approved = _memo(b"water")
    swapped = _memo(b"whisky")

    verdict = evaluate_tx(swapped, _receipt(message_binding(approved), "structural"))

    assert not verdict.approved
    assert "NOT the one the receipt attests" in verdict.reason


def test_a_failing_receipt_can_never_approve_anything() -> None:
    """Precedence matters: a matching binding on a receipt that REVERTED must not
    approve. The binding says 'this is the plan', not 'the plan is good'."""
    tx = _memo(b"water")

    verdict = evaluate_tx(
        tx, _receipt(message_binding(tx), "structural", status="fail")
    )

    assert not verdict.approved
    assert "did not pass" in verdict.reason


def test_a_receipt_with_no_binding_is_refused() -> None:
    """Older receipts predate the binding. Absent proof is not weak proof — it is none."""
    verdict = evaluate_tx(_memo(b"water"), _receipt(None, None))

    assert not verdict.approved
    assert "no binding" in verdict.reason


def test_requiring_exact_refuses_a_structural_receipt() -> None:
    """A gate that silently accepts a weaker proof than it demanded is not a gate."""
    tx = _memo(b"water")

    verdict = evaluate_tx(
        tx, _receipt(message_binding(tx), "structural"), require="exact"
    )

    assert not verdict.approved
    assert "requires an exact binding" in verdict.reason


def test_a_malformed_transaction_returns_a_verdict_rather_than_raising() -> None:
    """A signing gate that throws is a gate that gets wrapped in try/except and bypassed."""
    verdict = evaluate_tx("!!!not base64!!!", _receipt("deadbeef", "structural"))

    assert isinstance(verdict, SigningVerdict)
    assert not verdict.approved


def test_an_unrecognised_strength_is_refused() -> None:
    tx = _memo(b"water")

    verdict = evaluate_tx(tx, _receipt(message_binding(tx), "vibes"))

    assert not verdict.approved
    assert "strength" in verdict.reason


# --------------------------------------------------------------------------- #
# The Receipt carries it.
# --------------------------------------------------------------------------- #
def test_simulate_binds_the_receipt_to_what_it_simulated() -> None:
    from gecko.simulate import BuiltTx, simulate

    tx = _memo(b"water")
    rpc_calls: list[str] = []

    def fake_rpc(_url: str, method: str, _params: list) -> dict:
        rpc_calls.append(method)
        return {"result": {"value": {"err": None, "unitsConsumed": 42, "logs": []}}}

    receipt = simulate(
        {},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=fake_rpc,
        build_call=lambda _plan: BuiltTx(tx=tx, encoding="base64"),
    )

    assert receipt.binding_strength == "structural"
    assert receipt.message_binding == message_binding(tx)
    assert evaluate_tx(tx, receipt).approved


def test_an_undecodable_build_leaves_the_receipt_unbound() -> None:
    """A builder we cannot decode yields NO binding — and `evaluate_tx` then refuses,
    rather than the reverse."""
    from gecko.simulate import BuiltTx, simulate

    receipt = simulate(
        {},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=lambda *_a: {
            "result": {"value": {"err": None, "unitsConsumed": 1, "logs": []}}
        },
        build_call=lambda _plan: BuiltTx(
            tx=base64.b64encode(b"garbage").decode(), encoding="base64"
        ),
    )

    assert receipt.message_binding is None
    assert not evaluate_tx("anything", receipt).approved


def test_the_binding_survives_a_dataclass_replace() -> None:
    """Receipts get copied in reporting paths; the binding must ride along."""
    tx = _memo(b"water")
    receipt = _receipt(message_binding(tx), "structural")

    assert evaluate_tx(tx, dataclasses.replace(receipt, units_consumed=1)).approved


# --------------------------------------------------------------------------- #
# v0 + a real blockhash — what makes an `exact` binding reachable.
# --------------------------------------------------------------------------- #
def test_a_lookup_table_produces_a_versioned_message() -> None:
    """A legacy message caps out near 35 accounts and 1232 bytes; a multi-hop route
    exceeds that routinely. Anything carrying tables must compile as v0 or it won't fit."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    table = AddressLookupTableAccount(
        key=Pubkey.from_string(USDC), addresses=[Pubkey.from_string(OTHER)]
    )

    built = assemble_unsigned_tx(
        [Instruction(program, b"x", [])], PAYER, lookup_tables=[table]
    )

    from gecko.txbind import _decode, _message_of

    _message, version = _message_of(_decode(built.tx, "base64"))
    assert version == "v0"


def test_a_legacy_and_a_v0_message_never_share_a_binding() -> None:
    """The version is folded into the digest, so two messages with identical contents but
    different wire formats cannot be mistaken for one another."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    instructions = [Instruction(program, b"same", [])]
    table = AddressLookupTableAccount(
        key=Pubkey.from_string(USDC), addresses=[Pubkey.from_string(OTHER)]
    )

    legacy = assemble_unsigned_tx(instructions, PAYER)
    versioned = assemble_unsigned_tx(instructions, PAYER, lookup_tables=[table])

    assert message_binding(legacy.tx) != message_binding(versioned.tx)


def test_a_real_blockhash_changes_only_the_exact_binding() -> None:
    """Structural is blockhash-blind BY DESIGN — that is what makes it stable across the
    substitution simulateTransaction performs. Exact is not."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    instructions = [Instruction(program, b"x", [])]
    hash_a = "11111111111111111111111111111111"
    hash_b = "So11111111111111111111111111111111111111112"

    a = assemble_unsigned_tx(instructions, PAYER, blockhash=hash_a)
    b = assemble_unsigned_tx(instructions, PAYER, blockhash=hash_b)

    assert message_binding(a.tx, strength="structural") == message_binding(
        b.tx, strength="structural"
    )
    assert message_binding(a.tx, strength="exact") != message_binding(
        b.tx, strength="exact"
    )


def test_simulating_without_replacement_earns_an_exact_binding() -> None:
    from gecko.simulate import BuiltTx, simulate

    tx = _memo(b"water")
    seen: dict = {}

    def fake_rpc(_url: str, _method: str, params: list) -> dict:
        seen.update(params[1])
        return {"result": {"value": {"err": None, "unitsConsumed": 1, "logs": []}}}

    receipt = simulate(
        {},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=fake_rpc,
        build_call=lambda _plan: BuiltTx(tx=tx, encoding="base64"),
        replace_blockhash=False,
    )

    assert seen["replaceRecentBlockhash"] is False
    assert receipt.binding_strength == "exact"
    assert evaluate_tx(tx, receipt, require="exact").approved
