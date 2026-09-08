"""The human-verifiable binding prefix.

The prefix exists because the binding travels in the same tool result as the bytes it
covers — so on its own it compares a value against itself. A person reading sixteen
characters on one screen and confirming them on another is the only second origin the
flow has. These tests pin the shape a human compares, and the refusals that keep a
truncated or non-hex value from being rendered as though it were one.
"""

from __future__ import annotations

import pytest

from gecko.txbind import binding_prefix, message_binding

DIGEST = "8bd548e86cd06d60de20de419d0b1b2f" + "a" * 32


def test_it_renders_grouped_uppercase_hex() -> None:
    assert binding_prefix(DIGEST) == "8BD5-48E8-6CD0-6D60"


def test_it_is_stable_and_case_insensitive() -> None:
    """The same binding must read identically wherever a person sees it."""
    assert binding_prefix(DIGEST.upper()) == binding_prefix(DIGEST)
    assert binding_prefix(f"  {DIGEST}  ") == binding_prefix(DIGEST)


def test_two_different_transactions_read_differently() -> None:
    """A prefix nobody can tell apart is decoration, not a check."""
    import base64

    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    payer = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")
    program = Pubkey.from_string("Vote111111111111111111111111111111111111111")

    def tx(data: bytes) -> str:
        instruction = Instruction(
            program, data, [AccountMeta(payer, is_signer=True, is_writable=True)]
        )
        message = Message.new_with_blockhash([instruction], payer, Hash.default())
        return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()

    one = binding_prefix(message_binding(tx(b"\x01" * 8), strength="exact"))
    two = binding_prefix(message_binding(tx(b"\x02" * 8), strength="exact"))
    assert one != two


def test_a_truncated_binding_is_refused_rather_than_padded() -> None:
    """A short prefix would be compared exactly as confidently as a full one."""
    with pytest.raises(ValueError, match="too short"):
        binding_prefix("8bd548")


def test_a_non_hex_value_is_refused() -> None:
    """Rendering a prefix of something that is not a digest would dress it as one."""
    with pytest.raises(ValueError, match="hexadecimal"):
        binding_prefix("z" * 64)
