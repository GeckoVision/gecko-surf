"""The instruction encoding — the half comprehension was missing.

WHY THIS EXISTS, in a live agent's own words. Handed PDA recipes and no encoding, it
declined to build the call:

    I'm not going to hand-roll the byte layout for an instruction that moves USDC into a
    launch vault — a wrong discriminator against a real program is exactly the failure
    your simulate-then-bind flow exists to prevent, and doing it manually skips that flow
    entirely.

Declining was right. Not having to decline is better, and the IDL was already fetched.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import pytest

from gecko.artifact import instruction_encoding

CONTRIBUTE = {
    "name": "contribute",
    "discriminator": [82, 33, 68, 131, 32, 0, 205, 95],
    "args": [
        {"name": "requested_amount", "type": "u64"},
        {"name": "min_accepted_amount", "type": "u64"},
    ],
}


def test_the_two_sources_agree_and_that_agreement_is_reported() -> None:
    """Anchor writes the discriminator into the IDL AND it is sha256("global:<name>")[:8].
    Two independent answers to the same question is evidence, and saying so is the point —
    `verified` means more than `declared`."""
    encoding = instruction_encoding(CONTRIBUTE)

    assert encoding["discriminator"] == CONTRIBUTE["discriminator"]
    assert encoding["discriminator_source"] == "verified"
    assert encoding["discriminator_hex"] == "522144832000cd5f"


def test_the_encoding_reproduces_bytes_mainnet_ACCEPTED() -> None:
    """Not "the bytes we expected" — the bytes a real program took. This exact payload
    simulated on mainnet at 21,368 CU."""
    encoding = instruction_encoding(CONTRIBUTE)

    assembled = bytes(encoding["discriminator"])
    values = {"requested_amount": 100_000, "min_accepted_amount": 100_000}
    for arg in encoding["args"]:
        assembled += struct.pack("<Q", values[arg["name"]])

    assert assembled.hex() == "522144832000cd5fa086010000000000a086010000000000"


def test_an_idl_without_a_discriminator_falls_back_to_the_convention() -> None:
    """Pre-0.30 IDLs omit it. The convention is the answer, and it is labelled as such
    rather than passed off as the program's own word."""
    encoding = instruction_encoding({"name": "contribute", "args": []})

    assert encoding["discriminator_source"] == "computed"
    assert encoding["discriminator"] == list(
        hashlib.sha256(b"global:contribute").digest()[:8]
    )


def test_a_DISAGREEMENT_is_never_resolved_by_preference() -> None:
    """THE SAFETY PROPERTY. If the IDL's discriminator is not the conventional one, the
    surface disagrees with itself — and either choice may be the wrong call against the
    real program. Picking one silently is how a well-formed transaction fails, or worse
    succeeds against something the caller did not mean.
    """
    lying = {**CONTRIBUTE, "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}

    encoding = instruction_encoding(lying)

    assert encoding["discriminator_source"] == "disagree"
    assert encoding["declared"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert encoding["computed"] == list(
        hashlib.sha256(b"global:contribute").digest()[:8]
    )
    assert "Do not pick one" in encoding["warning"]


@pytest.mark.parametrize(
    "declared,rendered,size",
    [
        ("u64", "u64", 8),
        ("u8", "u8", 1),
        ("pubkey", "pubkey", 32),
        ({"array": ["u8", 32]}, "[u8; 32]", 32),
        ({"defined": {"name": "LaunchParams"}}, "LaunchParams", None),
        ({"option": "u64"}, "Option<u64>", None),
        ({"vec": "u8"}, "Vec<u8>", None),
        ("string", "string", None),
    ],
)
def test_a_variable_length_type_reports_no_size_rather_than_a_guess(
    declared: Any, rendered: str, size: int | None
) -> None:
    """`fixed_size: null` is a real answer: a String or a Vec is length-prefixed and
    cannot be sized without the value. A guessed length is worse than none."""
    encoding = instruction_encoding(
        {"name": "x", "args": [{"name": "a", "type": declared}]}
    )

    assert encoding["args"][0]["type"] == rendered
    assert encoding["args"][0]["fixed_size"] == size


def test_variable_length_arguments_are_called_out() -> None:
    encoding = instruction_encoding(
        {"name": "x", "args": [{"name": "memo", "type": "string"}]}
    )
    assert "variable-length" in encoding["note"]


def test_argument_ORDER_is_preserved_because_borsh_has_no_field_names() -> None:
    """Borsh writes fields positionally. Reordering these silently swaps two u64s — the
    amounts, in the live case — and produces a transaction that is valid and wrong."""
    encoding = instruction_encoding(CONTRIBUTE)
    assert [a["name"] for a in encoding["args"]] == [
        "requested_amount",
        "min_accepted_amount",
    ]
