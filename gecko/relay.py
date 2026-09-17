"""Accepting a fee payer's co-signature without trusting the fee payer.

A gasless purchase has two signers and they are different parties. The buyer authorises
the spend; a relay (Kora, in this repo's demo) pays the network fee and signs as
``account_keys[0]``. Gecko holds neither key. It hands the SAME unsigned bytes to each,
takes back what they return, and assembles one transaction — :mod:`gecko.cosign` does the
assembly and refuses anything whose message is not byte-identical to the one handed out.

THIS MODULE EXISTS BECAUSE THE RELAY DOES NOT RETURN THE SAME BYTES. Read from Kora's
source (``crates/lib/src/lighthouse/assertion.rs``, ``add_fee_payer_assertion``): when
Lighthouse is enabled and the relay is signing rather than sending, it APPENDS one
instruction to the message before signing it, an assertion that its own balance ends the
transaction no lower than ``balance - estimated_fee``. That instruction is the relay's
whole protection against a transaction that drains it through a program Kora cannot
parse, which is why ``examples/kora_demo/kora.gecko.toml`` turns it on and why it is not
negotiable. So the message that comes back is the message we sent plus one instruction,
and every byte-identical check downstream, ``cosign.take_signature`` and
``signer._rebind`` alike, refuses it. Correctly: those checks compare bytes, and the bytes
differ.

The rule here, then, is not "byte-identical" but the narrowest thing that is still true:

    THE RELAY MAY APPEND. IT MAY NOT ALTER, REORDER, REMOVE, REPLACE THE PAYER, MOVE THE
    BLOCKHASH, ADD A SIGNER, OR WRITE TO ANYTHING.

Every one of those is checked below by name, over the DECODED message rather than the
serialized bytes, because the comparison that matters is "does every instruction we
authored still say what it said" and a byte compare cannot answer that once one
instruction has been appended. Anything appended must belong to a program the caller
permitted (the Lighthouse program, by default, and nothing else) and may only READ.

WHAT HAPPENS AFTER ACCEPTANCE, and why it is not this module's job. An accepted
transaction is a NEW subject: a different message, so a different binding, so the receipt
taken over the original attests nothing about it. The caller re-simulates it, re-verifies
it, and runs the spend gate over it before the buyer is asked to sign. That ordering is
:func:`gecko.autonomous_purchase.run_purchase`'s to keep, and it is the reason the relay
signs FIRST: a buyer signature taken over the original message would be invalid over the
extended one, and a buyer signature taken after extension covers the assertion too.

This module signs nothing, holds no key, and talks to no relay. :class:`FeePayerRelay`
is the shape of the party that does, implemented outside ``gecko/`` (``scripts/`` holds
the Kora client), and :func:`sponsor` is the one place it is called. Refusals carry a code
and never a transaction body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .cosign import CosignRefused, _decode, signature_slots

__all__ = [
    "LIGHTHOUSE_ASSERT_ACCOUNT_INFO",
    "LIGHTHOUSE_PROGRAM",
    "FeePayerRelay",
    "RelayAccepted",
    "RelayRefusal",
    "RelayRefused",
    "accept_relay_signature",
    "sponsor",
]

#: Lighthouse, the assertion program Kora appends to. Pinned from Kora's own constant
#: (``crates/lib/src/constant.rs``); an assertion from any other program is refused.
LIGHTHOUSE_PROGRAM = "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95"
#: The one Lighthouse instruction Kora emits: ``AssertAccountInfo``, discriminator 5. The
#: spend gate allowlists exactly this prefix and nothing wider.
LIGHTHOUSE_ASSERT_ACCOUNT_INFO = b"\x05"

RelayRefusal = Literal[
    "relay-unavailable",
    "relay-returned-nothing",
    "undecodable-transaction",
    "fee-payer-changed",
    "blockhash-changed",
    "signers-changed",
    "instruction-removed",
    "instruction-altered",
    "account-permission-changed",
    "extension-not-permitted",
    "extension-writes",
    "relay-signature-missing",
    "relay-signature-invalid",
]


class RelayRefused(Exception):
    """The relay's answer could not be accepted. A code, a reason, never bytes."""

    def __init__(self, code: RelayRefusal, reason: str) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason


@runtime_checkable
class FeePayerRelay(Protocol):
    """The party that pays the fee. Implemented OUTSIDE ``gecko/``, always.

    Two members, like :class:`~gecko.signer.SigningBackend`: which account it signs for,
    and a call that takes unsigned bytes and returns a transaction. There is no member
    through which a key could arrive, and no member that broadcasts.
    """

    @property
    def pubkey(self) -> str:
        """The account the relay signs as. Must be ``account_keys[0]`` of what it is sent."""
        ...

    def sign_as_fee_payer(self, unsigned_transaction_base64: str) -> str:
        """Sign as fee payer and return the FULL transaction, base64. Never send it."""
        ...


@dataclass(frozen=True)
class RelayAccepted:
    """A relay-signed transaction that passed every check. The NEW subject."""

    #: The relay's transaction, base64, with its signature in slot 0 and (possibly) an
    #: appended assertion. This is what gets re-simulated, re-verified and buyer-signed.
    transaction_base64: str
    fee_payer: str
    relay_signature: bytes
    #: Programs the relay appended instructions for, in order. Empty when it appended
    #: nothing, which is legal and means the message is byte-identical to the original.
    appended_programs: tuple[str, ...]

    @property
    def extended(self) -> bool:
        return bool(self.appended_programs)


@dataclass(frozen=True)
class _Ix:
    program: str
    accounts: tuple[str, ...]
    data: bytes


def _resolved(message: Any) -> tuple[list[str], tuple[_Ix, ...], set[str]]:
    """Keys, instructions resolved to pubkeys, and the writable set, for either version."""
    keys = [str(key) for key in message.account_keys]
    lookups = getattr(message, "address_table_lookups", None)
    if lookups:
        # A message that loads accounts from a table addresses accounts this module cannot
        # see; the same refusal gecko.txbind makes, for the same reason.
        raise RelayRefused(
            "undecodable-transaction",
            "the message loads accounts from a lookup table, so the accounts its "
            "instructions touch cannot be resolved here",
        )
    header = message.header
    required = int(header.num_required_signatures)
    ro_signed = int(header.num_readonly_signed_accounts)
    ro_unsigned = int(header.num_readonly_unsigned_accounts)
    writable: set[str] = set()
    for index, key in enumerate(keys):
        if index < required - ro_signed:
            writable.add(key)
        elif required <= index < len(keys) - ro_unsigned:
            writable.add(key)
    instructions = []
    for compiled in message.instructions:
        try:
            program = keys[int(compiled.program_id_index)]
            accounts = tuple(keys[int(i)] for i in bytes(compiled.accounts))
        except IndexError:
            raise RelayRefused(
                "undecodable-transaction",
                "an instruction indexes an account the message does not carry",
            ) from None
        instructions.append(_Ix(program, accounts, bytes(compiled.data)))
    return keys, tuple(instructions), writable


def accept_relay_signature(
    original_base64: str,
    returned_base64: str,
    *,
    relay_pubkey: str,
    permitted_extensions: frozenset[str] = frozenset({LIGHTHOUSE_PROGRAM}),
) -> RelayAccepted:
    """Is ``returned`` the transaction we sent, signed by the relay, plus at most a read-only
    assertion? Refuses by name otherwise; never repairs, never trims.

    ``permitted_extensions`` names the programs an appended instruction may belong to.
    The default is Lighthouse alone. An empty set demands byte-identity, which is what a
    relay with Lighthouse disabled produces.
    """
    if not returned_base64:
        raise RelayRefused(
            "relay-returned-nothing", "the relay returned no transaction"
        )
    try:
        original = _decode(original_base64)
        returned = _decode(returned_base64)
    except CosignRefused as exc:
        raise RelayRefused("undecodable-transaction", exc.reason) from None

    before, before_ix, before_writable = _resolved(original.message)
    after, after_ix, after_writable = _resolved(returned.message)

    if not before or before[0] != relay_pubkey:
        raise RelayRefused(
            "fee-payer-changed",
            "the bytes handed to the relay do not name it as fee payer; nothing to accept",
        )
    if not after or after[0] != before[0]:
        raise RelayRefused(
            "fee-payer-changed",
            "the relay returned a transaction whose fee payer is not the one it was sent",
        )
    if str(returned.message.recent_blockhash) != str(original.message.recent_blockhash):
        raise RelayRefused(
            "blockhash-changed",
            "the relay returned a transaction carrying a different blockhash; the "
            "receipt taken over the original would attest nothing about it",
        )
    try:
        if signature_slots(returned_base64) != signature_slots(original_base64):
            raise RelayRefused(
                "signers-changed",
                "the relay returned a transaction whose required signers differ from "
                "the ones it was sent",
            )
    except CosignRefused as exc:
        raise RelayRefused("undecodable-transaction", exc.reason) from None

    if len(after_ix) < len(before_ix):
        raise RelayRefused(
            "instruction-removed",
            f"the relay returned {len(after_ix)} instructions for the {len(before_ix)} "
            f"it was sent",
        )
    for position, (ours, theirs) in enumerate(zip(before_ix, after_ix)):
        if ours != theirs:
            raise RelayRefused(
                "instruction-altered",
                f"instruction {position} came back different from the one we authored "
                f"(program {ours.program} -> {theirs.program})",
            )
    for key in before:
        if (key in before_writable) != (key in after_writable):
            raise RelayRefused(
                "account-permission-changed",
                f"the relay changed whether {key} is writable",
            )

    appended = after_ix[len(before_ix) :]
    for extra in appended:
        if extra.program not in permitted_extensions:
            raise RelayRefused(
                "extension-not-permitted",
                f"the relay appended an instruction for {extra.program}, which is not a "
                f"program a fee payer may add",
            )
    # Writability is a property of the MESSAGE, not of one instruction: the assertion
    # names the fee payer, and the fee payer is writable because it pays. So "the
    # extension may read, never write" is checked as "the extension widens the writable
    # set by nothing": every account it introduced is read-only, and every account we
    # authored kept its permission (checked above).
    introduced = [key for key in after if key not in set(before)]
    writes = [key for key in introduced if key in after_writable]
    if writes:
        raise RelayRefused(
            "extension-writes",
            f"the relay's addition introduces writable accounts ({', '.join(writes)}); "
            f"a fee payer's addition may read, never write",
        )

    signature = list(returned.signatures)[0]
    if bytes(signature) == bytes(64):
        raise RelayRefused(
            "relay-signature-missing",
            "the relay returned the transaction with its own slot still empty",
        )
    from solders.pubkey import Pubkey

    if not signature.verify(Pubkey.from_string(relay_pubkey), bytes(returned.message)):
        raise RelayRefused(
            "relay-signature-invalid",
            "the relay's signature does not verify against the message it returned",
        )
    return RelayAccepted(
        transaction_base64=returned_base64,
        fee_payer=before[0],
        relay_signature=bytes(signature),
        appended_programs=tuple(extra.program for extra in appended),
    )


def sponsor(
    unsigned_transaction_base64: str,
    relay: FeePayerRelay,
    *,
    permitted_extensions: frozenset[str] = frozenset({LIGHTHOUSE_PROGRAM}),
) -> RelayAccepted:
    """Ask ``relay`` to sign as fee payer, then accept or refuse what it returns.

    A relay fault is a refusal, never a fall-back: nothing here reaches for a second
    payer. The exception type only travels into the reason; a relay's message is
    untrusted text that may carry a path or a key.
    """
    try:
        returned = relay.sign_as_fee_payer(unsigned_transaction_base64)
    except RelayRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - any relay fault is a refusal
        raise RelayRefused(
            "relay-unavailable",
            f"the relay did not produce a signature ({type(exc).__name__})",
        ) from None
    return accept_relay_signature(
        unsigned_transaction_base64,
        returned,
        relay_pubkey=relay.pubkey,
        permitted_extensions=permitted_extensions,
    )
