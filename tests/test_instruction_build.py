"""The encoder takes an IDL, not a program — and refuses every type it cannot encode.

THE ACCEPTANCE TEST FOR THE ABSTRACTION is `test_a_second_program_builds_with_no_engine
_change`: pump.fun's `sell` is encoded and assembled from the IDL committed under
``tests/fixtures/orquestra/``, with nothing imported from ``gecko/providers/`` and nothing
added to ``gecko/instruction_build.py``. If a second program ever needs a branch in the
engine module, this file is where that shows up first.

The other half is the refusals. An encoder that guesses a layout produces a well-formed
transaction against the wrong bytes — it may simulate, it may land, and nothing downstream
catches it. So every type this does not know RAISES, naming itself: the standing gap is
pump.fun's ``OptionBool``, a program-declared struct whose layout is not in the
instruction's own args.
"""

from __future__ import annotations

import base64
import json
import pathlib
from typing import Any

import pytest

from gecko.instruction_build import (
    InstructionEncodeError,
    build_unsigned_instruction,
    encode_instruction_data,
    instruction_accounts,
    make_instruction,
)

PUMP_IDL = pathlib.Path("tests/fixtures/orquestra/6i6q26bmm46b89xlxo1kv/idl.json")
SOME_KEY = "GpaLFMwQWh2xuBkMQGKmcYT5A1WgYJekofu6DJjp8W9c"
OTHER_KEY = "6Q5Ki322q5tRxyzfU9uAeAXsN3DzoMV8W9C8xAVTtiq9"


def pump_instruction(name: str) -> dict[str, Any]:
    idl = json.loads(PUMP_IDL.read_text())["idl"]
    for instruction in idl["instructions"]:
        if instruction["name"] == name:
            return dict(instruction)
    raise AssertionError(f"{name} is not in the fixture IDL")


def ix(args: list[dict[str, Any]], name: str = "do_thing") -> dict[str, Any]:
    """A minimal IDL instruction carrying just the arg layout under test."""
    return {"name": name, "accounts": [{"name": "only"}], "args": args}


def data(args: list[dict[str, Any]], values: dict[str, Any]) -> bytes:
    """The encoded data WITHOUT the 8-byte discriminator."""
    return encode_instruction_data(ix(args), values)[8:]


# --- the second program, and the point of the whole split -------------------------------


def test_a_second_program_builds_with_no_engine_change() -> None:
    """pump.fun `sell`, from its own IDL, through the same engine — no new code anywhere.

    Nothing in this test imports a provider module. The program's ABI arrives as DATA and
    the engine encodes it: that is the whole claim the split makes.
    """
    sell = pump_instruction("sell")
    program = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    slots = instruction_accounts(sell)

    # The IDL's order and flags, untouched — `user` is the one signer, `program` is pinned.
    assert [s.name for s in slots][:7] == [
        "global",
        "fee_recipient",
        "mint",
        "bonding_curve",
        "associated_bonding_curve",
        "associated_user",
        "user",
    ]
    assert [s.name for s in slots if s.is_signer] == ["user"]
    assert {s.name for s in slots if s.address} == {
        "system_program",
        "program",
        "fee_program",
    }

    # Two u64s, little-endian, after the IDL's own discriminator.
    encoded = encode_instruction_data(sell, {"amount": 1_000, "min_sol_output": 7})
    assert encoded.hex() == (
        "33e685a4017f83ad"  # sha256("global:sell")[:8], and what the IDL declares
        + (1_000).to_bytes(8, "little").hex()
        + (7).to_bytes(8, "little").hex()
    )

    # A caller supplies the derived slots; the pinned ones need nobody.
    supplied = {s.name: SOME_KEY for s in slots if not s.address}
    built = build_unsigned_instruction(
        sell,
        program_id=program,
        accounts=supplied,
        args={"amount": 1_000, "min_sol_output": 7},
        fee_payer=OTHER_KEY,
    )
    from solders.transaction import VersionedTransaction

    message = VersionedTransaction.from_bytes(base64.b64decode(built.tx)).message
    (compiled,) = message.instructions
    assert str(message.account_keys[compiled.program_id_index]) == program
    assert bytes(compiled.data) == encoded
    assert len(compiled.accounts) == len(slots)
    assert message.header.num_required_signatures == 2, "relay first, then the actor"


def test_the_engine_module_names_no_program() -> None:
    """The grep that keeps `providers/` from growing a second engine.

    A program id, a store name or a provider import inside the engine module is the exact
    failure this split exists to prevent, and it is cheaper to catch here than in review.
    """
    source = pathlib.Path("gecko/instruction_build.py").read_text()
    assert "providers" not in source.replace("``providers/``", "")
    assert "BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya" not in source
    assert "make_purchase" not in source


# --- what it encodes --------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared, value, expected",
    [
        ("bool", True, "01"),
        ("bool", False, "00"),
        ("u8", 255, "ff"),
        ("u16", 513, "0102"),
        ("u32", 1, "01000000"),
        ("u64", 2, "0200000000000000"),
        ("u128", 3, "03" + "00" * 15),
        ("i8", -1, "ff"),
        ("i16", -2, "feff"),
        ("i32", -3, "fdffffff"),
        ("i64", -4, "fcffffffffffffff"),
        ("i128", -5, "fb" + "ff" * 15),
        ("string", "hi", "02000000" + "6869"),
        ("string", "", "00000000"),
        ("bytes", b"\x01\x02", "02000000" + "0102"),
    ],
)
def test_the_scalars_encode_as_borsh(declared: str, value: Any, expected: str) -> None:
    assert data([{"name": "v", "type": declared}], {"v": value}).hex() == expected


def test_a_pubkey_encodes_as_its_32_raw_bytes() -> None:
    from solders.pubkey import Pubkey

    encoded = data([{"name": "v", "type": "pubkey"}], {"v": SOME_KEY})
    assert encoded == bytes(Pubkey.from_string(SOME_KEY))
    # the older IDL spelling means the same thing
    assert data([{"name": "v", "type": "publicKey"}], {"v": SOME_KEY}) == encoded


def test_an_option_carries_its_presence_byte() -> None:
    declared = [{"name": "v", "type": {"option": "u8"}}]
    assert data(declared, {"v": None}).hex() == "00"
    assert data(declared, {"v": 7}).hex() == "0107"


def test_a_vec_carries_a_u32_count_then_its_elements() -> None:
    declared = [{"name": "v", "type": {"vec": "u16"}}]
    assert data(declared, {"v": []}).hex() == "00000000"
    assert data(declared, {"v": [1, 2]}).hex() == "02000000" + "0100" + "0200"


def test_a_fixed_array_carries_no_length_prefix() -> None:
    declared = [{"name": "v", "type": {"array": ["u8", 3]}}]
    assert data(declared, {"v": [1, 2, 3]}).hex() == "010203"


def test_the_args_are_encoded_in_idl_order_not_caller_order() -> None:
    declared = [{"name": "a", "type": "u8"}, {"name": "b", "type": "u8"}]
    # the caller's dict is ordered b-then-a; the wire must not be
    assert data(declared, {"b": 2, "a": 1}).hex() == "0102"


def test_an_extra_arg_the_idl_does_not_declare_is_ignored() -> None:
    declared = [{"name": "a", "type": "u8"}]
    assert data(declared, {"a": 1, "unknown": "whatever"}).hex() == "01"


# --- what it refuses, by name -----------------------------------------------------------


def test_a_defined_type_is_refused_by_its_own_name() -> None:
    """The standing gap, and it must stay loud: pump.fun `buy` takes an `OptionBool`.

    It is a program-declared struct; its layout is in the IDL's `types` section, not in the
    instruction's args, and inventing one byte for it would be a guess that simulates.
    """
    with pytest.raises(InstructionEncodeError, match="OptionBool"):
        encode_instruction_data(
            pump_instruction("buy"),
            {"amount": 1, "max_sol_cost": 2, "track_volume": True},
        )


@pytest.mark.parametrize("declared", ["f32", "f64", "Foo", "u256"])
def test_an_unknown_scalar_is_refused_by_its_own_name(declared: str) -> None:
    with pytest.raises(InstructionEncodeError, match=declared):
        data([{"name": "v", "type": declared}], {"v": 1})


def test_a_nested_defined_type_is_named_through_its_container() -> None:
    declared = [{"name": "v", "type": {"vec": {"defined": {"name": "Hop"}}}}]
    with pytest.raises(InstructionEncodeError, match="Hop"):
        data(declared, {"v": [{}]})


@pytest.mark.parametrize(
    "declared, value",
    [
        ("u8", 256),
        ("u8", -1),
        ("u8", True),
        ("u8", "1"),
        ("i8", 128),
        ("u64", 1 << 64),
        ("bool", 1),
        ("string", 7),
        ("pubkey", "not-a-key"),
        ({"vec": "u8"}, "bytes-are-not-a-vec"),
        ({"array": ["u8", 3]}, [1, 2]),
    ],
)
def test_a_value_that_does_not_fit_its_type_is_refused(
    declared: Any, value: Any
) -> None:
    with pytest.raises(InstructionEncodeError):
        data([{"name": "v", "type": declared}], {"v": value})


def test_a_missing_arg_is_named_and_never_defaulted() -> None:
    with pytest.raises(InstructionEncodeError, match="`b`"):
        data([{"name": "a", "type": "u8"}, {"name": "b", "type": "u8"}], {"a": 1})


def test_a_string_too_large_for_any_transaction_is_refused() -> None:
    with pytest.raises(InstructionEncodeError, match="1232"):
        data([{"name": "v", "type": "string"}], {"v": "x" * 2000})


def test_a_discriminator_the_surface_disagrees_with_itself_about_is_refused() -> None:
    """Neither answer is picked. An IDL that contradicts Anchor's convention may be a
    renamed instruction or a stale artifact, and either choice is a call into the dark."""
    instruction = ix([], name="sell")
    instruction["discriminator"] = [0, 1, 2, 3, 4, 5, 6, 7]
    with pytest.raises(InstructionEncodeError, match="disagrees with itself"):
        encode_instruction_data(instruction, {})


def test_a_missing_account_is_named_and_never_skipped() -> None:
    sell = pump_instruction("sell")
    with pytest.raises(InstructionEncodeError, match="`global`"):
        make_instruction(
            sell,
            program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            accounts={},
            args={"amount": 1, "min_sol_output": 1},
        )


def test_a_nested_account_group_is_refused_rather_than_flattened() -> None:
    grouped = {
        "name": "compose",
        "accounts": [{"name": "inner", "accounts": [{"name": "a"}]}],
        "args": [],
    }
    with pytest.raises(InstructionEncodeError, match="nested account group"):
        instruction_accounts(grouped)


def test_a_build_without_a_fee_payer_is_refused() -> None:
    with pytest.raises(InstructionEncodeError, match="fee payer"):
        build_unsigned_instruction(
            pump_instruction("sell"),
            program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            accounts={},
            args={},
            fee_payer="",
        )
