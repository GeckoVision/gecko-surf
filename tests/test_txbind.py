"""``evaluate_tx`` — the receipt→signature binding.

Offline (Pattern B): messages are assembled locally, no RPC, no network. The behaviours
that matter here are the refusals — a signing gate is worth exactly what it refuses, and
every "we could not check" path must render as NO, never as silence.
"""

from __future__ import annotations

import base64
import dataclasses
from typing import Any

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


#: A REAL, non-zero blockhash. Every D3 fixture below uses one on purpose: a landing
#: bundle is built with ``Hash.default()`` (landing.py), so on it the byte at offset 68
#: and the byte at the blockhash offset are BOTH zero and zeroing either yields identical
#: bytes — a digest comparison then cannot tell a right offset from a wrong one.
REAL_BLOCKHASH = "So11111111111111111111111111111111111111112"
TABLE = "GnMsEEyF6XKMajwtBsjxcBv8QoEM71QyUzz4Lf7vkeRu"


def _memo_with(payload: bytes, *, blockhash: str, account: str = USDC) -> str:
    """The same one-memo transaction, with the blockhash and the touched account chosen.

    ``account == blockhash`` is the attack fixture: a message whose recent_blockhash bytes
    also appear as an account key.
    """
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(
        pubkey=Pubkey.from_string(account), is_signer=False, is_writable=False
    )
    return assemble_unsigned_tx(
        [Instruction(program, payload, [meta])], PAYER, blockhash=blockhash
    ).tx


def _receipt(
    binding: str | None,
    strength: str | None,
    status: str = "pass",
    network: str = "mainnet",
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
        network=network,  # type: ignore[arg-type]
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

    verdict = evaluate_tx(
        tx, _receipt(message_binding(tx), "structural"), expected_network="mainnet"
    )

    assert verdict.approved
    assert "blockhash NOT covered" in verdict.reason  # the caveat always travels


def test_a_different_transaction_is_refused() -> None:
    approved = _memo(b"water")
    swapped = _memo(b"whisky")

    verdict = evaluate_tx(
        swapped,
        _receipt(message_binding(approved), "structural"),
        expected_network="mainnet",
    )

    assert not verdict.approved
    assert "NOT the one the receipt attests" in verdict.reason


def test_a_failing_receipt_can_never_approve_anything() -> None:
    """Precedence matters: a matching binding on a receipt that REVERTED must not
    approve. The binding says 'this is the plan', not 'the plan is good'."""
    tx = _memo(b"water")

    verdict = evaluate_tx(
        tx,
        _receipt(message_binding(tx), "structural", status="fail"),
        expected_network="mainnet",
    )

    assert not verdict.approved
    assert "did not pass" in verdict.reason


def test_a_receipt_with_no_binding_is_refused() -> None:
    """Older receipts predate the binding. Absent proof is not weak proof — it is none."""
    verdict = evaluate_tx(
        _memo(b"water"), _receipt(None, None), expected_network="mainnet"
    )

    assert not verdict.approved
    assert "no binding" in verdict.reason


def test_requiring_exact_refuses_a_structural_receipt() -> None:
    """A gate that silently accepts a weaker proof than it demanded is not a gate."""
    tx = _memo(b"water")

    verdict = evaluate_tx(
        tx,
        _receipt(message_binding(tx), "structural"),
        require="exact",
        expected_network="mainnet",
    )

    assert not verdict.approved
    assert "requires an exact binding" in verdict.reason


def test_a_malformed_transaction_returns_a_verdict_rather_than_raising() -> None:
    """A signing gate that throws is a gate that gets wrapped in try/except and bypassed."""
    verdict = evaluate_tx(
        "!!!not base64!!!",
        _receipt("deadbeef", "structural"),
        expected_network="mainnet",
    )

    assert isinstance(verdict, SigningVerdict)
    assert not verdict.approved


def test_an_unrecognised_strength_is_refused() -> None:
    tx = _memo(b"water")

    verdict = evaluate_tx(
        tx, _receipt(message_binding(tx), "vibes"), expected_network="mainnet"
    )

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
    assert evaluate_tx(tx, receipt, expected_network="mainnet").approved


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
    assert not evaluate_tx("anything", receipt, expected_network="mainnet").approved


def test_the_binding_survives_a_dataclass_replace() -> None:
    """Receipts get copied in reporting paths; the binding must ride along."""
    tx = _memo(b"water")
    receipt = _receipt(message_binding(tx), "structural")

    assert evaluate_tx(
        tx, dataclasses.replace(receipt, units_consumed=1), expected_network="mainnet"
    ).approved


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
    assert evaluate_tx(
        tx, receipt, require="exact", expected_network="mainnet"
    ).approved


# --------------------------------------------------------------------------- #
# D3 — the blockhash offset is DERIVED from the layout, never searched for by value.
#
# The old normaliser did `serialized.find(blockhash)`. Any 32 bytes that happen to equal
# the blockhash — an account key, a slice of instruction data — win the race, and the
# normaliser then zeroes THAT and leaves the real blockhash inside the "blockhash-blind"
# digest. The structural binding stops being structural exactly when an attacker gets to
# choose the bytes.
# --------------------------------------------------------------------------- #
def _body(tx: str) -> tuple[Any, str, bytes]:
    """The message object, its version, and the bytes the binding actually hashes."""
    from gecko.txbind import _decode, _message_of

    message, version = _message_of(_decode(tx, "base64"))
    return message, version, bytes(message)


def test_the_blockhash_offset_is_derived_not_searched_for() -> None:
    """Pin the OFFSET, not a byte comparison. `serialized[offset:offset+32] == blockhash`
    is satisfied at a wrong offset whenever those bytes are duplicated (see the attack
    fixture below), so only the offset itself can distinguish a correct parse."""
    from gecko.txbind import blockhash_offset

    message, version, body = _body(_memo_with(b"x", blockhash=REAL_BLOCKHASH))

    # legacy: 3 header bytes + 1 compact-u16 count byte + 3 account keys × 32.
    assert len(message.account_keys) == 3
    assert blockhash_offset(body, version) == 100
    assert blockhash_offset(body, version) == 3 + 1 + 32 * len(message.account_keys)
    assert body[100:132] == bytes(message.recent_blockhash)


def test_a_versioned_message_offset_skips_no_section() -> None:
    """v0 carries an address-table-lookup section AFTER the instructions; the blockhash
    still sits right behind the static keys, and the parse must consume the tail."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    from gecko.txbind import blockhash_offset

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    table = AddressLookupTableAccount(
        key=Pubkey.from_string(USDC), addresses=[Pubkey.from_string(OTHER)]
    )
    built = assemble_unsigned_tx(
        [
            Instruction(
                program,
                b"x",
                [
                    AccountMeta(
                        pubkey=Pubkey.from_string(OTHER),
                        is_signer=False,
                        is_writable=True,
                    )
                ],
            )
        ],
        PAYER,
        blockhash=REAL_BLOCKHASH,
        lookup_tables=[table],
    )
    message, version, body = _body(built.tx)

    assert version == "v0"
    assert len(message.account_keys) == 2  # the third address comes from the table
    assert blockhash_offset(body, version) == 68
    assert body[68:100] == bytes(message.recent_blockhash)


def test_a_blockhash_that_equals_an_account_key_zeroes_the_blockhash_not_the_key() -> (
    None
):
    """THE ATTACK FIXTURE. `recent_blockhash` is chosen to equal an account key already in
    the message, so a value search lands on the key. Reproduced on pre-D3 code: `find`
    returns 68 (the third account key) and the real blockhash stays in the digest."""
    from gecko.txbind import _normalise_blockhash

    message, _version, body = _body(_memo_with(b"x", blockhash=USDC, account=USDC))
    blockhash = bytes(message.recent_blockhash)

    assert bytes(message.account_keys[2]) == blockhash  # the collision, on purpose
    assert body.find(blockhash) == 68  # the trap the old normaliser walked into

    normalised = _normalise_blockhash(message, body)

    assert normalised[68:100] == blockhash  # the ACCOUNT KEY survives …
    assert normalised[100:132] == bytes(32)  # … and the BLOCKHASH is what got zeroed


def test_the_attack_fixture_still_binds_blockhash_blind() -> None:
    """Behaviour, not offsets: same plan, same touched account, different blockhash → the
    SAME structural digest. Pre-D3 the collision fixture leaked its blockhash into the
    digest, so these two differed."""
    collided = _memo_with(b"x", blockhash=USDC, account=USDC)
    ordinary = _memo_with(b"x", blockhash=REAL_BLOCKHASH, account=USDC)

    assert message_binding(collided, strength="structural") == message_binding(
        ordinary, strength="structural"
    )


def test_swapping_an_account_key_changes_the_structural_digest() -> None:
    """The other half of the pair: the keys ARE covered. (True on pre-D3 code too — it is
    the blockhash-blindness above that the old normaliser broke.)"""
    touching_usdc = _memo_with(b"x", blockhash=REAL_BLOCKHASH, account=USDC)
    touching_other = _memo_with(b"x", blockhash=REAL_BLOCKHASH, account=OTHER)

    assert message_binding(touching_usdc, strength="structural") != message_binding(
        touching_other, strength="structural"
    )


def test_a_parse_landing_on_the_wrong_bytes_is_refused() -> None:
    """The self-verifying assertion: if the derived offset does not hold the message's own
    blockhash, we refuse instead of zeroing bytes we do not understand."""
    from gecko.txbind import _normalise_blockhash

    message, _version, body = _body(_memo_with(b"x", blockhash=REAL_BLOCKHASH))
    tampered = bytearray(body)
    tampered[100] ^= 0xFF

    with pytest.raises(TxDecodeError, match="blockhash"):
        _normalise_blockhash(message, bytes(tampered))


def test_an_unparseable_message_raises_rather_than_returning_a_sentinel() -> None:
    """A sentinel offset (or a sentinel digest) would make two unparseable messages EQUAL
    at the comparison in `evaluate_tx` and therefore APPROVE. The no-raise contract lives
    in `evaluate_tx`, never here."""
    from gecko.txbind import blockhash_offset

    with pytest.raises(TxDecodeError):
        blockhash_offset(b"\x01\x00\x01", "legacy")  # truncated: no key count


def test_trailing_bytes_are_refused_rather_than_hashed() -> None:
    from gecko.txbind import blockhash_offset

    _message, version, body = _body(_memo_with(b"x", blockhash=REAL_BLOCKHASH))

    with pytest.raises(TxDecodeError, match="trailing"):
        blockhash_offset(body + b"\x00", version)


def test_an_absurd_account_count_is_refused_without_reading_it() -> None:
    """Bounded: a message claiming 65,535 account keys is refused on the count, not after
    walking 2MB of a buffer that does not exist."""
    from gecko.txbind import blockhash_offset

    claimed_huge = b"\x01\x00\x01" + b"\xff\xff\x03"  # header + compact-u16(65535)

    with pytest.raises(TxDecodeError, match="account keys"):
        blockhash_offset(claimed_huge, "legacy")


def test_a_multi_byte_length_prefix_is_walked_correctly() -> None:
    """Instruction data over 127 bytes needs a two-byte compact-u16 length. A parser that
    mis-steps there lands somewhere plausible and would still satisfy a byte comparison;
    the offset and the end-of-buffer check are what catch it."""
    from gecko.txbind import blockhash_offset

    message, version, body = _body(_memo_with(b"z" * 300, blockhash=REAL_BLOCKHASH))

    assert blockhash_offset(body, version) == 100
    assert body[100:132] == bytes(message.recent_blockhash)
    with pytest.raises(TxDecodeError, match="trailing"):
        blockhash_offset(body + b"\x00", version)


def test_a_versioned_prefix_on_a_legacy_message_is_refused() -> None:
    """The high bit of the first byte marks a versioned message; on a legacy layout it is
    the signature count and must be clear."""
    from gecko.txbind import blockhash_offset

    _message, _version, body = _body(_memo_with(b"x", blockhash=REAL_BLOCKHASH))

    with pytest.raises(TxDecodeError):
        blockhash_offset(b"\x80" + body[1:], "legacy")


def _simulated(tx: str, *, network: str = "mainnet") -> Receipt:
    """A passing Receipt produced by our own ``simulate`` over ``tx``."""
    from gecko.simulate import BuiltTx, simulate

    def fake_rpc(_url: str, _method: str, _params: list) -> dict:
        return {"result": {"value": {"err": None, "unitsConsumed": 1, "logs": []}}}

    return simulate(
        {},
        rpc_url="http://127.0.0.1:8899",
        rpc_call=fake_rpc,
        build_call=lambda _plan: BuiltTx(tx=tx, encoding="base64"),
        network=network,  # type: ignore[arg-type]
    )


def _v0_through_table(resolved: str, *, table: str = TABLE) -> str:
    """A v0 transaction whose only non-static account is loaded FROM ``table``.

    ``resolved`` is the address the table is holding at simulate time. Change it and
    the transaction executes against a different account — with, as the tests below
    prove, not one byte of difference on the wire.
    """
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    target = Pubkey.from_string(resolved)
    lookup = AddressLookupTableAccount(
        key=Pubkey.from_string(table), addresses=[target]
    )
    meta = AccountMeta(pubkey=target, is_signer=False, is_writable=False)
    return assemble_unsigned_tx(
        [Instruction(program, b"route", [meta])], PAYER, lookup_tables=[lookup]
    ).tx


def test_a_message_that_loads_accounts_from_a_table_earns_no_binding() -> None:
    from gecko.txbind import UnresolvedLookupError

    with pytest.raises(UnresolvedLookupError) as caught:
        message_binding(_v0_through_table(OTHER))

    assert "lookup table" in str(caught.value)
    # A subclass of TxDecodeError, so every existing catcher already fails closed.
    assert isinstance(caught.value, TxDecodeError)


def test_a_receipt_for_one_route_cannot_approve_another_with_the_same_bytes() -> None:
    """THE fail-open this task exists to close, end to end.

    The Receipt comes from our own ``simulate`` over route A. The subject is route B:
    the same bytes, a different account. A digest over the message can never tell them
    apart, so the only honest verdict is a refusal — for BOTH of them.
    """
    route_a = _v0_through_table(OTHER)
    route_b = _v0_through_table(USDC)
    assert base64.b64decode(route_a) == base64.b64decode(route_b)

    receipt = _simulated(route_a)
    verdict = evaluate_tx(route_b, receipt, expected_network="mainnet")

    assert verdict.approved is False
    assert "lookup table" in verdict.reason
    # And not by luck: the receipt it was simulated against refuses too.
    assert evaluate_tx(route_a, receipt, expected_network="mainnet").approved is False


def test_a_receipt_over_static_accounts_records_that_it_resolved_none() -> None:
    receipt = _simulated(_memo(b"water"))

    assert receipt.lookup_resolution == "none"
    assert receipt.message_binding is not None


def test_an_undecodable_transaction_still_refuses_rather_than_raising() -> None:
    """The lookup check runs inside the decode path; it must not turn the gate into
    something that throws."""
    receipt = _simulated(_memo(b"water"))

    assert (
        evaluate_tx("not-base64!!", receipt, expected_network="mainnet").approved
        is False
    )


def test_an_unresolved_receipt_refuses_even_a_matching_binding() -> None:
    """Belt and braces, on the Receipt side. If a Receipt ever reaches the gate marked
    unresolved, no digest agreement can rescue it — including one computed over a
    perfectly ordinary legacy message."""
    tx = _memo(b"water")
    receipt = dataclasses.replace(
        _receipt(message_binding(tx), "structural"), lookup_resolution="unresolved"
    )

    verdict = evaluate_tx(tx, receipt, expected_network="mainnet")

    assert verdict.approved is False
    assert "lookup table" in verdict.reason


def test_simulate_records_the_unresolved_lookup_rather_than_a_binding() -> None:
    """The Receipt says WHY it carries no binding. A Receipt that simply lacked one
    would be indistinguishable from an undecodable build, and the two want different
    fixes."""
    receipt = _simulated(_v0_through_table(OTHER))

    assert receipt.lookup_resolution == "unresolved"
    assert receipt.message_binding is None
    assert receipt.binding_strength is None


def test_the_new_receipt_field_never_reaches_the_corpus() -> None:
    """Invariant #1: the corpus is categorical control plane. ``lookup_resolution`` is
    a gate input, not an outcome, and the projection must not have grown a field."""
    from gecko.corpus import simulated_outcome_from

    outcome = simulated_outcome_from(
        _simulated(_memo(b"water")),
        program_id="MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        instruction="memo",
        recipe_hash="0" * 64,
        slot=None,
        network="fork",
        ts=0,
        surface_id="test",
    )

    assert not hasattr(outcome, "lookup_resolution")
    assert "lookup" not in repr(outcome)


def test_the_refusal_keys_on_the_lookups_not_on_the_version() -> None:
    """A v0 message with an EMPTY lookup section commits to every account it uses, so
    it binds and approves. Refusing all of v0 would be a blunter rule that breaks a
    legitimate transaction — and the pressure to relax a rule that over-refuses is how
    gates get deleted."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    table = AddressLookupTableAccount(
        key=Pubkey.from_string(TABLE), addresses=[Pubkey.from_string(OTHER)]
    )
    # No account meta resolves through the table, so try_compile emits no lookup.
    built = assemble_unsigned_tx(
        [Instruction(program, b"x", [])], PAYER, lookup_tables=[table]
    )

    from gecko.txbind import _decode, _message_of

    message, version = _message_of(_decode(built.tx, "base64"))
    assert version == "v0"
    assert len(message.address_table_lookups) == 0

    receipt = _simulated(built.tx)
    assert receipt.lookup_resolution == "none"
    assert evaluate_tx(built.tx, receipt, expected_network="mainnet").approved is True


def test_two_routes_over_one_table_are_byte_identical_and_are_not_the_same_call() -> (
    None
):
    """The premise, pinned before anything is asserted about the gate. If this ever
    stops holding, the refusal below is guarding a case that no longer exists — and a
    reader deserves to see that in a test name rather than infer it."""
    route_a = _v0_through_table(OTHER)
    route_b = _v0_through_table(USDC)

    assert base64.b64decode(route_a) == base64.b64decode(route_b)
    assert OTHER != USDC


# --------------------------------------------------------------------------- #
# D2 — the network. A binding proves WHICH MESSAGE; it proves nothing about WHERE
# the simulation ran, and a fork snapshot is not the state a mainnet signature lands in.
# --------------------------------------------------------------------------- #
def _on(network: str, binding: str | None, strength: str | None = "structural"):
    """A passing receipt that ASSERTS ``network`` — nothing else stands in the way."""
    return dataclasses.replace(_receipt(binding, strength), network=network)


def test_a_fork_receipt_cannot_clear_a_mainnet_signature() -> None:
    """THE D2 BUG. The bytes match, the binding matches, the receipt passed — and the
    state it was validated against is a snapshot nobody is committing into."""
    tx = _memo(b"buy water")

    verdict = evaluate_tx(
        tx, _on("fork", message_binding(tx)), expected_network="mainnet"
    )

    assert verdict.approved is False
    assert "fork" in verdict.reason and "mainnet" in verdict.reason


def test_the_same_receipt_clears_the_network_it_actually_ran_on() -> None:
    tx = _memo(b"buy water")

    assert evaluate_tx(
        tx, _on("fork", message_binding(tx)), expected_network="fork"
    ).approved


def test_a_receipt_with_no_network_attribute_at_all_is_refused() -> None:
    """MISSING == UNKNOWN == REFUSE. ``evaluate_tx`` reads the receipt through
    ``getattr`` on an ``Any``, so a Receipt-SHAPED object from older or third-party code
    reaches the gate with no ``network`` at all. Skipping the check there would make the
    gate strictest on the receipts it built itself and blind to every other one."""

    class LooksLikeAReceipt:
        status = "pass"
        binding_strength = "structural"
        lookup_resolution = "none"

        def __init__(self, binding: str) -> None:
            self.message_binding = binding

    tx = _memo(b"buy water")
    verdict = evaluate_tx(
        tx, LooksLikeAReceipt(message_binding(tx)), expected_network="mainnet"
    )

    assert verdict.approved is False
    assert "network" in verdict.reason.lower()


@pytest.mark.parametrize(
    ("receipt_network", "expected_network"),
    [
        ("unknown", "mainnet"),
        ("mainnet", "unknown"),
        ("unknown", "unknown"),
        ("other", "other"),
        ("solana-mainnet-beta", "solana-mainnet-beta"),
    ],
)
def test_nothing_outside_the_approvable_set_is_ever_approved(
    receipt_network: str, expected_network: str
) -> None:
    """Affirmative comparison only: BOTH sides must be a named, approvable member and
    the same one. Never ``if expected and actual and expected != actual`` — that
    approves whenever either side is empty, which is exactly when nothing is known.

    The last pair is an off-vocabulary string on both sides: two receipts that both said
    "solana-mainnet-beta" would otherwise compare equal and approve on a vocabulary
    nothing validates."""
    tx = _memo(b"buy water")

    verdict = evaluate_tx(
        tx, _on(receipt_network, message_binding(tx)), expected_network=expected_network
    )

    assert verdict.approved is False


def test_the_network_parameter_has_no_default() -> None:
    """mypy forces the decision at every call site; this asserts it at runtime so a
    later 'convenience' default fails loudly rather than silently reopening D2."""
    tx = _memo(b"buy water")

    with pytest.raises(TypeError):
        evaluate_tx(tx, _on("fork", message_binding(tx)), expected_network="mainnet")  # type: ignore[call-arg]


def test_a_receipt_simulate_built_without_an_assertion_approves_nothing() -> None:
    """``simulate``'s ``network`` default is the fail-closed member, not a permissive
    one: a Receipt from a caller who asserted nothing refuses against EVERY expectation,
    including ``unknown``. That is what makes a default admissible here at all."""
    tx = _memo(b"water")
    receipt = _simulated(tx, network="unknown")

    assert receipt.network == "unknown"
    for expected in ("mainnet", "devnet", "fork", "unknown"):
        assert evaluate_tx(tx, receipt, expected_network=expected).approved is False
