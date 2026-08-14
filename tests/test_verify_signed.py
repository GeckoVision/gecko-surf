"""Did the signer sign the transaction we checked?

Gecko hands back unsigned bytes and a receipt binding them. Someone else signs — PayBox,
Phantom's MCP, Privy's agent CLI, a wallet — and all of them take the same primitive: our
base64 in, their signed base64 out. Between those two moments nothing of ours runs, and
the bytes could have been swapped for others that sign just as cleanly.

This tool closes the loop for whoever holds the binding. It cannot prevent a bad
signature; it makes one DETECTABLE BEFORE BROADCAST, which is the strongest thing a party
holding no key can honestly do.

Two facts are reported SEPARATELY and deliberately:

* ``binding_matches`` — are these the bytes that were checked?
* ``signed`` — has anyone actually signed them?

Because the binding covers the MESSAGE and a signature is not part of the message, an
UNSIGNED echo binds perfectly. A single boolean would call that a pass, and it is the
exact hole `scripts/privy_backend.py` closes at the signer for the same reason.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

pytest.importorskip("solders")

from gecko.landing import assemble_unsigned_tx  # noqa: E402
from gecko.txbind import message_binding  # noqa: E402
from gecko.verify_signed import (  # noqa: E402
    VERIFY_SIGNED_TOOL,
    verify_signed_result,
)

PAYER = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _tx(payload: bytes = b"gecko") -> str:
    """A real unsigned transaction, base64 — one memo instruction."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    program = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    meta = AccountMeta(Pubkey.from_string(USDC), False, False)
    return assemble_unsigned_tx([Instruction(program, payload, [meta])], PAYER).tx


def _sign(tx_b64: str) -> str:
    """Stamp a non-zero first signature — what a signer returns, without a signer.

    The signature is not verified by anything here and could not be: verifying it needs
    the signer's public key, which is exactly what a substituted transaction would also
    supply. The binding is over the message; this only makes the 64-byte slot non-empty.
    """
    raw = bytearray(base64.b64decode(tx_b64))
    raw[1:65] = bytes(range(64))  # after the 1-byte signature count
    return base64.b64encode(bytes(raw)).decode()


def _check(**args: Any) -> dict[str, Any]:
    return verify_signed_result(args)


# --- the loop closes ---------------------------------------------------------


def test_the_signed_bytes_we_checked_verify() -> None:
    unsigned = _tx()
    binding = message_binding(unsigned, strength="exact")

    out = _check(transaction=_sign(unsigned), binding=binding)

    assert out["verified"] is True
    assert out["binding_matches"] is True
    assert out["signed"] is True


def test_a_signature_does_not_change_the_binding() -> None:
    """The property the whole composition rests on. If signing changed the binding, no
    signer on earth could be verified against a receipt taken before it signed."""
    unsigned = _tx()

    assert message_binding(_sign(unsigned), strength="exact") == message_binding(
        unsigned, strength="exact"
    )


# --- the substitutions it exists to catch ------------------------------------


def test_different_bytes_do_not_verify() -> None:
    """The whole point: a signer handed back SOMETHING, and it is not what was checked."""
    binding = message_binding(_tx(b"the plan you approved"), strength="exact")

    out = _check(transaction=_sign(_tx(b"a different plan entirely")), binding=binding)

    assert out["verified"] is False
    assert out["binding_matches"] is False
    assert "does not match" in out["reason"]


def test_an_unsigned_echo_is_reported_as_unsigned_not_as_verified() -> None:
    """The hole a single boolean would open. A binding covers the MESSAGE, and a
    signature is not part of the message — so returning the transaction UNTOUCHED binds
    perfectly. These are the right bytes and nobody signed them, and those are two
    different facts that must not be collapsed into one."""
    unsigned = _tx()

    out = _check(
        transaction=unsigned, binding=message_binding(unsigned, strength="exact")
    )

    assert out["binding_matches"] is True  # they really are the checked bytes…
    assert out["signed"] is False  # …and nobody signed them
    assert out["verified"] is False  # so the answer is NO
    assert "not signed" in out["reason"]


def test_an_all_zero_signature_slot_does_not_count_as_signed() -> None:
    """The same hole in its other spelling: a signer that returns the transaction with an
    empty 64-byte slot. `scripts/privy_backend.py` refuses this at the signer; a verifier
    that missed it would bless what that refusal exists to stop."""
    unsigned = _tx()
    raw = bytearray(base64.b64decode(unsigned))
    raw[1:65] = bytes(64)  # explicitly zeroed
    zeroed = base64.b64encode(bytes(raw)).decode()

    assert (
        _check(transaction=zeroed, binding=message_binding(unsigned, strength="exact"))[
            "signed"
        ]
        is False
    )


# --- it refuses rather than crashing -----------------------------------------


def test_a_binding_of_the_wrong_shape_is_refused_cleanly() -> None:
    out = _check(transaction=_sign(_tx()), binding="not-a-hash")

    assert out["verified"] is False
    # The SPECIFIC reason, not merely a refusal. Without the shape check the caller still
    # gets a "does not match" — which contains the word "binding" and would satisfy a
    # looser assertion while telling them their malformed input was compared to something.
    assert "sha256 hex" in out["reason"]


def test_one_wrong_character_at_the_END_of_the_binding_is_caught() -> None:
    """Pins a FULL comparison. A prefix check passes every test built from two unrelated
    transactions, because their digests differ almost immediately — the only way to see
    the difference is a binding that agrees everywhere except the last character."""
    unsigned = _tx()
    binding = message_binding(unsigned, strength="exact")
    tampered = binding[:-1] + ("0" if binding[-1] != "0" else "1")

    assert _check(transaction=_sign(unsigned), binding=tampered)["verified"] is False


def test_a_transaction_whose_accounts_hide_in_a_lookup_table_is_refused() -> None:
    """No binding exists for a message whose account keys live in an address lookup
    table — resolving one needs a chain read this path does not make. The refusal must be
    explicit; treating "cannot bind" as "nothing to object to" would approve exactly the
    transactions whose contents we cannot see."""
    from gecko import verify_signed as module
    from gecko.txbind import UnresolvedLookupError

    def _raise(*_a: Any, **_kw: Any) -> str:
        # The message deliberately says NOTHING about lookup tables. `UnresolvedLookupError`
        # subclasses `TxDecodeError`, so a broken handler falls through to the decode
        # branch, which interpolates the exception's own text — and an exception whose
        # message named lookup tables would make that fallback satisfy the assertion below.
        raise UnresolvedLookupError("x")

    original = module.message_binding
    module.message_binding = _raise  # type: ignore[assignment]
    try:
        out = _check(transaction=_sign(_tx()), binding="a" * 64)
    finally:
        module.message_binding = original  # type: ignore[assignment]

    assert out["verified"] is False
    assert "lookup table" in out["reason"]


def test_the_function_and_the_transport_agree_on_the_default() -> None:
    """One default, not two. The shim used to carry its own copy of `exact`, so changing
    the function's default silently changed nothing — and the drift that matters is toward
    `structural`, which does not cover the blockhash."""
    from gecko.verify_signed import DEFAULT_STRENGTH, verify_signed

    unsigned = _tx()
    structural = message_binding(unsigned, strength="structural")

    assert DEFAULT_STRENGTH == "exact"
    # Called DIRECTLY, bypassing the shim: a structural binding must not satisfy it.
    assert verify_signed(_sign(unsigned), structural).verified is False
    assert _check(transaction=_sign(unsigned), binding=structural)["verified"] is False


def test_undecodable_bytes_are_refused_cleanly() -> None:
    out = _check(transaction="!!!not base64!!!", binding="a" * 64)

    assert out["verified"] is False
    assert out["reason"]


def test_missing_arguments_are_refused_cleanly() -> None:
    assert _check()["verified"] is False
    assert _check(transaction=_tx())["verified"] is False
    assert _check(binding="a" * 64)["verified"] is False


def test_the_strength_is_stated_by_the_caller_never_defaulted_to_the_weak_one() -> None:
    """`structural` does not cover the blockhash, so it would approve a transaction
    re-stamped onto a different one. Callers get `exact` unless they ask otherwise — the
    repo's rule that the safe value is the default and the weak one must be chosen."""
    unsigned = _tx()
    structural = message_binding(unsigned, strength="structural")

    # An `exact` caller must NOT be satisfied by a structural binding…
    assert _check(transaction=_sign(unsigned), binding=structural)["verified"] is False
    # …and asking for structural explicitly is allowed, and says so in the result.
    out = _check(
        transaction=_sign(unsigned), binding=structural, binding_strength="structural"
    )
    assert out["verified"] is True
    assert out["binding_strength"] == "structural"


def test_an_unknown_strength_is_refused_rather_than_coerced() -> None:
    out = _check(transaction=_sign(_tx()), binding="a" * 64, binding_strength="vibes")

    assert out["verified"] is False
    assert "strength" in out["reason"]


# --- the tool definition -----------------------------------------------------


def test_the_tool_declares_what_it_needs_and_holds_nothing() -> None:
    schema = VERIFY_SIGNED_TOOL["inputSchema"]

    assert VERIFY_SIGNED_TOOL["name"] == "verify_signed_transaction"
    assert set(schema["required"]) == {"transaction", "binding"}
    # No key, no account, no network credential — it reads bytes and compares a hash.
    assert set(schema["properties"]) == {
        "transaction",
        "binding",
        "binding_strength",
    }
