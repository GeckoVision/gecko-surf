"""``evaluate_tx`` — bind a Receipt to the exact message a signer is about to sign.

Until now a Receipt attested that *some* plan simulated clean. It could not attest that
**this** transaction is that plan. That gap is the difference between "we simulated
something like this" and "we simulated exactly this," and it is the whole reason a
pre-signature check is worth anything: without it, everything between the simulation and
the signature is unverified, and that window is exactly where a swapped account or a
rewritten route would live.

Three decisions carry this module.

**Hash the MESSAGE, not the transaction.** A Solana transaction is signatures + message.
Our simulated transaction has zeroed signature slots; the signed one does not. Hashing the
whole transaction would break the instant it is signed — the binding has to survive
signing to be useful, so it covers the message, which is precisely the bytes a signature
commits to.

**Two binding strengths, named honestly.** ``simulateTransaction`` is called with
``replaceRecentBlockhash: true``, so the blockhash we sent is NOT the one the simulation
ran against. A fork simulation therefore cannot honestly attest a blockhash:

* ``structural`` — covers the header, the account keys, the instructions and their order,
  and the fee payer, with the blockhash normalised out. This is what a simulation with a
  replaced blockhash can truthfully claim, and it catches a swapped account, an added
  instruction, or a reordered route. The blockhash's position is DERIVED from the message
  layout (:func:`blockhash_offset`), never found by searching for its value — whoever
  chooses the blockhash would otherwise choose which 32 bytes get normalised away.
* ``exact`` — covers the whole message including the blockhash. Stronger, available only
  when the simulation ran against a real blockhash it did not replace, and it expires with
  that blockhash (~150 slots, roughly a minute).

Calling a structural binding "exact" would be the lie that makes the feature worthless, so
the strength travels with the verdict and every caller DEMANDS one: ``evaluate_tx``'s
``require`` has no default, because the weak value fails open (it approves a blockhash
nobody simulated) and the strong one refuses every fork simulation. A path about to
produce a signature demands ``exact`` and accepts that the receipt expires with its
blockhash.

**The network is compared, asserted on both sides, and never inferred.** A binding proves
WHICH MESSAGE; it says nothing about WHERE the simulation ran. A fork receipt used to
clear a mainnet signature — same bytes, same digest, and a snapshot of state nobody is
committing into. The gate compares the receipt's structured, closed-vocabulary ``network``
against one the caller must name. It never reads the Receipt's free-text honesty caveat
and never reads an RPC URL (a fork proxy answers at any hostname). Approval is membership
in an EXPLICIT approvable set on both sides plus equality — so the catch-all approves
nothing, including against itself, because equality is not agreement when neither side was
told anything.

WHY THE PROSE IS NOT AN INPUT — the full reasoning, with the string that proves it, lives
in :mod:`gecko.networks` and is deliberately not repeated here: this module must not so
much as NAME the prose field, or the next reader will reach for it. Read that module
docstring before touching anything below. ``tests/test_network_vocabulary.py`` greps this
file for the field name to keep it that way; that grep is a lint against the accidental
case, defeated by any string construction, and it is not a guarantee.

**Fail closed.** An undecodable transaction, an unknown version, or a receipt with no
binding is ``approved=False``. "We could not check" must never render as "fine" — that is
the failure a signing gate exists to prevent.

**A v0 message does not commit to the accounts it loads from a lookup table.** This is the
one place where "the bytes are identical" stops meaning "the transaction is identical". A
versioned message serializes its STATIC account keys, then, per lookup, the table's own
address and two lists of u8 INDEXES. The addresses those indexes resolve to live in the
table account on chain. Point one table key at different contents and you get two
transactions that are byte-for-byte equal, hash-for-hash equal, and execute against
different accounts — the exact shape a Jupiter route takes.

Resolving them would take an RPC read at verify time, which would make an RPC a trust root
of the signing gate and this module something that can fail open when a node is unreachable
or lying. So the rule here is the fail-closed one: a message with a NON-EMPTY lookup
section earns no binding at all, and any receipt marked ``unresolved`` refuses. The key is
the lookup section, never the version — a v0 message with no lookups commits to every
account it uses and binds normally, and a gate that over-refuses is a gate someone deletes.

This module reads and compares. It does not sign, does not send, and holds no key.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from .networks import APPROVABLE_NETWORKS, Network

__all__ = [
    "DISCRIMINATOR_CEILING",
    "BindingStrength",
    "DecodedInstruction",
    "DecodedMessage",
    "LookupResolution",
    "MessageVersion",
    "SigningVerdict",
    "TxDecodeError",
    "UnresolvedLookupError",
    "blockhash_offset",
    "decode_message",
    "evaluate_tx",
    "lookup_resolution_of",
    "message_binding",
]

#: How much of the message the binding covers. Ordered weakest → strongest.
BindingStrength = Literal["structural", "exact"]

#: The two message wire formats we decode. Single source of truth for this module and its
#: tests — never redeclare it.
MessageVersion = Literal["legacy", "v0"]

#: Whether a message loads any account through an address lookup table. Single source of
#: truth for this module, ``simulate.Receipt.lookup_resolution`` and their tests — never
#: redeclare it.
#:
#: * ``none`` — every account the message uses is in the message. The binding covers all
#:   of them, which is what makes it worth checking.
#: * ``unresolved`` — the message loads accounts from a table whose contents the bytes do
#:   not carry. No honest binding exists, so the gate refuses.
#:
#: There is deliberately no ``pinned`` member yet: it would mean the resolved address list
#: was captured at simulate time and re-checked at verify time, and nothing in this repo
#: can re-check it without an RPC. Add the member when the check exists, not before.
LookupResolution = Literal["none", "unresolved"]

_ZERO_BLOCKHASH = bytes(32)

# Bounds for the layout walk below. These are wire facts, not guesses: a transaction is
# capped at 1232 bytes on the wire, and every account reference inside a message is a u8
# index, so 256 static keys is the hard ceiling. The instruction and lookup caps are
# generous relative to what fits in 1232 bytes; they exist so a corrupt length prefix
# costs us a refusal rather than a walk through a buffer that was never there.
_MAX_MESSAGE_BYTES = 1232
_MAX_ACCOUNT_KEYS = 256
_MAX_INSTRUCTIONS = 256
_MAX_INDEX_LIST = 256
_MAX_LOOKUPS = 64


class TxDecodeError(Exception):
    """A transaction could not be decoded. Never carries the payload."""


class UnresolvedLookupError(TxDecodeError):
    """The message loads accounts through an address lookup table.

    A subclass of :class:`TxDecodeError` on purpose: every existing caller already treats
    that as "no binding / refuse", so this closes the hole in the callers we have and in
    the ones nobody has written yet. It is a distinct type only so a caller that wants to
    say WHY can, never so a caller can decide it does not matter.
    """


@dataclass(frozen=True)
class SigningVerdict:
    """Whether this exact transaction is the one a Receipt attests.

    ``approved`` is the only field a gate should branch on; ``reason`` is a short,
    value-free explanation for a human reading a log.
    """

    approved: bool
    reason: str
    strength: BindingStrength | None = None
    presented: str | None = None
    attested: str | None = None


#: Bitcoin/Solana base58 alphabet. Orquestra returns transactions base58-encoded, and
#: until now the RPC decoded them for us — we never needed it locally. Fifteen lines of
#: stdlib beats a dependency for that.
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: value for value, char in enumerate(_B58)}


def _b58decode(text: str) -> bytes:
    """Decode base58, preserving leading zero bytes (encoded as leading '1's)."""
    number = 0
    for char in text:
        digit = _B58_INDEX.get(char)
        if digit is None:
            raise ValueError("non-base58 character")
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    padding = len(text) - len(text.lstrip("1"))
    return b"\x00" * padding + body


def _decode(tx: str | bytes, encoding: str) -> bytes:
    """Raw transaction bytes from whatever the builder handed back."""
    if isinstance(tx, bytes):
        return tx
    try:
        if encoding == "base58":
            return _b58decode(tx)
        return base64.b64decode(tx, validate=True)
    except Exception as exc:  # noqa: BLE001 - redact: never echo the transaction
        raise TxDecodeError(f"transaction is not valid {encoding}") from exc


def _version_of(message: Any) -> MessageVersion:
    """Which wire layout this message object serializes to."""
    return "v0" if type(message).__name__.endswith("V0") else "legacy"


def _message_of(raw: bytes) -> tuple[Any, MessageVersion]:
    """The message and its version, for a legacy or a versioned transaction."""
    try:
        from solders.transaction import VersionedTransaction

        parsed = VersionedTransaction.from_bytes(raw)
        message = parsed.message
        return message, _version_of(message)
    except Exception:
        pass
    try:
        from solders.transaction import Transaction

        return Transaction.from_bytes(raw).message, "legacy"
    except Exception as exc:  # noqa: BLE001 - redact
        raise TxDecodeError("transaction decodes as neither legacy nor v0") from exc


def _lookup_count(message: Any) -> int:
    """How many address-table lookups this message carries.

    A legacy message has no lookup section at all, so it is ``0`` by construction and
    every account it uses is in the message. An unreadable section is a decode failure,
    never a ``0`` — "we could not tell" must not spell "there are none".
    """
    lookups = getattr(message, "address_table_lookups", None)
    if lookups is None:
        return 0
    try:
        return len(lookups)
    except TypeError as exc:
        raise TxDecodeError("message has an unreadable lookup section") from exc


def lookup_resolution_of(
    tx: str | bytes, *, encoding: str = "base64"
) -> LookupResolution:
    """Does ``tx`` load any account from an address lookup table?

    Callers use this to record on a Receipt WHY it carries no binding. Raises
    :class:`TxDecodeError` on a transaction it cannot decode: an undecodable build makes
    no claim in either direction, and returning ``none`` there would be a lie shaped
    exactly like the safe answer.
    """
    message, _version = _message_of(_decode(tx, encoding))
    return "unresolved" if _lookup_count(message) else "none"


#: How many leading instruction-data bytes are kept as the DISCRIMINATOR.
#:
#: Eight, because that is what Anchor dispatches on (``sha256("global:<name>")[:8]``).
#: Programs with a shorter selector — SPL Token's single byte, System's u32 — are covered
#: because an allowlist entry declares its own length and is matched as a PREFIX of these
#: bytes, so the author picks the selector width their program actually uses. Nothing
#: beyond eight bytes is kept: a discriminator is a routing decision, and the argument
#: bytes after it are payload this control plane has no business holding.
DISCRIMINATOR_CEILING = 8


@dataclass(frozen=True)
class DecodedInstruction:
    """One instruction, reduced to the facts a policy may be evaluated over.

    ``discriminator`` is the leading (at most :data:`DISCRIMINATOR_CEILING`) data bytes and
    is ``b""`` when the instruction carries no data at all. Empty is NOT a member of the
    vocabulary: an instruction that names no selector cannot be matched against an allowlist
    entry, and a caller must treat it as unreadable rather than as "the default one".
    """

    program_id: str
    discriminator: bytes
    data_length: int
    account_indexes: tuple[int, ...]


@dataclass(frozen=True)
class DecodedMessage:
    """A message reduced to program ids, selectors, writable accounts and the fee payer.

    This is the SUBJECT a spend policy is evaluated over. It is derived here, locally, from
    the same bytes :func:`message_binding` hashes — never from an agent's description of
    them and never from an RPC read. The lookup-table rule from the module docstring applies
    unchanged: a message that loads accounts from a table raises
    :class:`UnresolvedLookupError`, because the addresses those u8 indexes resolve to are
    not in the bytes and fetching them would make a node a trust root of a signing decision.

    ``writable_accounts`` is derived from the message header, which is where writability is
    declared. It INCLUDES the fee payer (index 0, always writable); a caller that wants
    "everything this transaction may write except the account paying for it" subtracts
    ``fee_payer`` itself and says so.
    """

    version: MessageVersion
    fee_payer: str
    account_keys: tuple[str, ...]
    writable_accounts: frozenset[str]
    instructions: tuple[DecodedInstruction, ...]


def decode_message(tx: str | bytes, *, encoding: str = "base64") -> DecodedMessage:
    """Decode ``tx`` into the facts a policy may read. Raises rather than guessing.

    Every failure is a :class:`TxDecodeError`: an unreadable header, a program index past
    the account list, a message with no instructions. None of them returns a partially
    populated :class:`DecodedMessage`, because a policy evaluated over half a message
    approves the half it could not see.
    """
    raw = _decode(tx, encoding)
    message, version = _message_of(raw)
    if _lookup_count(message):
        raise UnresolvedLookupError(
            "message loads accounts from an address lookup table — the bytes commit to "
            "the table and the indexes, never to the addresses they resolve to"
        )

    try:
        keys = tuple(str(key) for key in message.account_keys)
        header = message.header
        signers = int(header.num_required_signatures)
        readonly_signed = int(header.num_readonly_signed_accounts)
        readonly_unsigned = int(header.num_readonly_unsigned_accounts)
        compiled = list(message.instructions)
    except Exception as exc:  # noqa: BLE001 - redact: never echo the transaction
        raise TxDecodeError(
            "message does not expose a header, an account list and instructions"
        ) from exc

    total = len(keys)
    if total < 1:
        raise TxDecodeError("message declares no account keys")
    if (
        signers < 1
        or signers > total
        or readonly_signed > signers
        or signers + readonly_unsigned > total
    ):
        raise TxDecodeError("message header disagrees with its account key list")

    # Solana's writability layout: writable signers first, then readonly signers, then
    # writable non-signers, then readonly non-signers. Derived from the header rather than
    # from any per-account flag, because the header is what the runtime reads.
    writable = frozenset(
        key
        for index, key in enumerate(keys)
        if index < signers - readonly_signed
        or (signers <= index < total - readonly_unsigned)
    )

    instructions: list[DecodedInstruction] = []
    for one in compiled:
        try:
            program_index = int(one.program_id_index)
            account_indexes = tuple(bytes(one.accounts))
            data = bytes(one.data)
        except Exception as exc:  # noqa: BLE001 - redact
            raise TxDecodeError(
                "instruction does not expose a program, an account list and data"
            ) from exc
        if program_index >= total:
            raise TxDecodeError("instruction names a program outside the account list")
        instructions.append(
            DecodedInstruction(
                program_id=keys[program_index],
                discriminator=data[:DISCRIMINATOR_CEILING],
                data_length=len(data),
                account_indexes=account_indexes,
            )
        )

    if not instructions:
        raise TxDecodeError("message carries no instructions")

    return DecodedMessage(
        version=version,
        fee_payer=keys[0],
        account_keys=keys,
        writable_accounts=writable,
        instructions=tuple(instructions),
    )


def _compact_u16(buf: bytes, pos: int) -> tuple[int, int]:
    """Read Solana's ShortVec length prefix: up to three bytes, seven bits each.

    Non-canonical encodings (a continuation byte contributing nothing, or a value past
    65,535) are refused — a decoder that accepts two spellings of one length lets the same
    bytes mean two different messages.
    """
    value = 0
    for group in range(3):
        if pos >= len(buf):
            raise TxDecodeError("message ends inside a length prefix")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << (group * 7)
        if not byte & 0x80:
            if group and not byte:
                raise TxDecodeError("non-canonical length prefix")
            if value > 0xFFFF:
                raise TxDecodeError("length prefix out of range")
            return value, pos
    raise TxDecodeError("length prefix longer than three bytes")


def _advance(buf: bytes, pos: int, count: int) -> int:
    """Step over ``count`` declared bytes, refusing to step past the end."""
    end = pos + count
    if end > len(buf):
        raise TxDecodeError("message ends inside a section it declared")
    return end


def blockhash_offset(serialized: bytes, version: MessageVersion) -> int:
    """Byte offset of the 32-byte ``recent_blockhash`` inside a serialized message.

    The offset is DERIVED by walking the layout — optional version prefix, three header
    bytes, the compact-u16 account-key count, that many 32-byte keys — and the blockhash
    is the next 32 bytes. The rest of the message (instructions, and for v0 the
    address-table-lookup section) is walked too, not because the offset needs it but
    because a parse that does not reach exactly the end of the buffer did not understand
    the buffer, and this function must not return an offset it cannot stand behind.

    The previous implementation searched for the blockhash BY VALUE. Any 32 bytes that
    happened to equal it — an account key, a slice of instruction data — won that race,
    and the "blockhash-blind" structural digest then covered a zeroed account key and the
    real blockhash. Whoever picks the blockhash picks which bytes get normalised out, so
    the search had to go.

    Raises :class:`TxDecodeError` on anything it cannot fully account for. It never
    returns a sentinel: two unparseable messages that both mapped to the same sentinel
    would compare EQUAL in :func:`evaluate_tx` and approve. The no-raise contract of the
    signing gate lives in ``evaluate_tx``, not here.
    """
    if not serialized:
        raise TxDecodeError("empty message")
    if len(serialized) > _MAX_MESSAGE_BYTES:
        raise TxDecodeError("message is larger than a transaction may be on the wire")

    pos = 0
    # solders' ``bytes(MessageV0)`` omits the 0x80 version prefix that the same message
    # carries inside a serialized transaction. Accept either spelling, refuse a versioned
    # prefix on something decoded as legacy (there it is the signature count, and 128
    # signatures do not fit in a packet).
    if serialized[0] & 0x80:
        if version != "v0":
            raise TxDecodeError("versioned prefix on a message decoded as legacy")
        if serialized[0] & 0x7F != 0:
            raise TxDecodeError("unsupported message version")
        pos = 1

    if pos + 3 > len(serialized):
        raise TxDecodeError("message ends inside its header")
    signers = serialized[pos]
    readonly_signed = serialized[pos + 1]
    readonly_unsigned = serialized[pos + 2]
    pos += 3

    keys, pos = _compact_u16(serialized, pos)
    if keys < 1 or keys > _MAX_ACCOUNT_KEYS:
        raise TxDecodeError(
            f"message declares {keys} account keys, outside 1..{_MAX_ACCOUNT_KEYS}"
        )
    if (
        signers < 1
        or signers > keys
        or readonly_signed > signers
        or signers + readonly_unsigned > keys
    ):
        raise TxDecodeError("message header disagrees with its account key list")

    offset = _advance(serialized, pos, 32 * keys)
    pos = _advance(serialized, offset, 32)

    instructions, pos = _compact_u16(serialized, pos)
    if instructions > _MAX_INSTRUCTIONS:
        raise TxDecodeError(f"message declares {instructions} instructions")
    for _ in range(instructions):
        if pos >= len(serialized):
            raise TxDecodeError("message ends inside an instruction")
        program_index = serialized[pos]
        pos += 1
        if program_index >= keys:
            raise TxDecodeError("instruction names a program outside the account list")
        accounts, pos = _compact_u16(serialized, pos)
        if accounts > _MAX_INDEX_LIST:
            raise TxDecodeError("instruction declares too many accounts")
        pos = _advance(serialized, pos, accounts)
        data_len, pos = _compact_u16(serialized, pos)
        pos = _advance(serialized, pos, data_len)

    if version == "v0":
        lookups, pos = _compact_u16(serialized, pos)
        if lookups > _MAX_LOOKUPS:
            raise TxDecodeError("message declares too many address table lookups")
        for _ in range(lookups):
            pos = _advance(serialized, pos, 32)
            writable, pos = _compact_u16(serialized, pos)
            if writable > _MAX_INDEX_LIST:
                raise TxDecodeError("lookup declares too many writable indexes")
            pos = _advance(serialized, pos, writable)
            readonly, pos = _compact_u16(serialized, pos)
            if readonly > _MAX_INDEX_LIST:
                raise TxDecodeError("lookup declares too many readonly indexes")
            pos = _advance(serialized, pos, readonly)

    if pos != len(serialized):
        raise TxDecodeError("message has trailing bytes after its final section")
    return offset


def _normalise_blockhash(message: Any, serialized: bytes) -> bytes:
    """Zero the message's blockhash at its STRUCTURALLY derived offset.

    Fails closed: if the derived offset does not hold the message's own blockhash we
    refuse rather than zero bytes we do not understand.

    NOTE for a later reader: that equality check is a guard against a mis-stepped parser,
    NOT proof the offset is right. It passes at a wrong offset whenever those 32 bytes are
    duplicated in the message — exactly the case where ``recent_blockhash`` equals an
    account key, and trivially true for a landing bundle built with ``Hash.default()``,
    where the blockhash and much else are both zeros. Correctness comes from the
    derivation plus the pinned-offset tests in ``tests/test_txbind.py``; do not delete
    those as redundant with this line.
    """
    try:
        blockhash = bytes(message.recent_blockhash)
    except Exception as exc:  # noqa: BLE001 - redact
        raise TxDecodeError("message exposes no recent_blockhash") from exc

    offset = blockhash_offset(serialized, _version_of(message))
    if serialized[offset : offset + 32] != blockhash:
        raise TxDecodeError("derived blockhash offset does not hold the blockhash")
    return serialized[:offset] + _ZERO_BLOCKHASH + serialized[offset + 32 :]


def message_binding(
    tx: str | bytes,
    *,
    encoding: str = "base64",
    strength: BindingStrength = "structural",
) -> str:
    """The binding hash for a transaction's message.

    ``structural`` normalises the blockhash to zero first, so the digest is stable across
    the substitution ``simulateTransaction`` performs and across re-quoting the same plan.
    ``exact`` hashes the message verbatim.

    The version and the strength are folded into the digest input, so a legacy and a v0
    message with identical contents can never collide into the same binding, and a
    structural digest can never be mistaken for an exact one.

    Raises :class:`UnresolvedLookupError` for a message that loads accounts from an
    address lookup table. Returning a digest there would be the worst outcome available:
    a hash that looks like proof, matches, and says nothing about the accounts the
    transaction actually touches. There is no ``resolved_accounts`` parameter yet — when
    one is added, it belongs here, folded into the digest, not checked beside it.
    """
    raw = _decode(tx, encoding)
    message, version = _message_of(raw)
    if _lookup_count(message):
        raise UnresolvedLookupError(
            "message loads accounts from an address lookup table — the bytes commit to "
            "the table and the indexes, never to the addresses they resolve to"
        )
    body = bytes(message)
    if strength == "structural":
        body = _normalise_blockhash(message, body)

    digest = hashlib.sha256()
    digest.update(strength.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(version.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


def evaluate_tx(
    tx: str | bytes,
    receipt: Any,
    *,
    encoding: str = "base64",
    require: BindingStrength,
    expected_network: Network,
) -> SigningVerdict:
    """Is ``tx`` the transaction ``receipt`` attests, on the network the caller expects?

    ``require`` is the minimum strength the caller accepts. Asking for ``exact`` when the
    receipt carries only a structural binding is a REFUSAL, not a silent downgrade — a
    gate that accepts a weaker proof than it demanded is not a gate.

    It is keyword-only with NO DEFAULT, for the reason the default was wrong rather than
    merely untidy. ``structural`` is blockhash-blind by construction, so the old default
    approved a transaction carrying a blockhash NOBODY SIMULATED whenever a caller left
    the keyword off — the fail-open shaped exactly like the safe answer, one omission
    away from a signature. The safe-by-omission value is no better a default: ``exact``
    refuses every ``replaceRecentBlockhash: true`` simulation, which is most of them, and
    a gate that over-refuses is a gate someone deletes. There is no strength that is
    right for all callers, so the caller states one.

    A SIGNING SITE DEMANDS ``exact``. That is a demand on the call site, never a
    redefinition of ``structural`` — collapsing the blockhash into the structural digest
    would erase the one strength a fork simulation can honestly earn. The cost is real
    and intended: an exact binding dies with its blockhash (~150 slots), so a refreshed
    transaction REFUSES and the caller must re-simulate rather than reuse. A receipt is
    true for the state it was taken against; take it at the moment you sign, not the day
    before. Re-simulating is free.

    ``expected_network`` is the network the CALLER says this signature is headed for. It
    is keyword-only with NO DEFAULT on purpose: a ``None``-means-skip default leaves every
    call site holding the hole while the code reads as fixed, so mypy makes each caller
    decide. It is compared against the receipt's structured ``network`` field and against
    nothing else — never a free-text caveat, never a URL. See :mod:`gecko.networks` for
    why, including the exact string that makes reading the prose invert this gate.

    Approval needs BOTH sides in :data:`~gecko.networks.APPROVABLE_NETWORKS` AND equal.
    A missing attribute, an off-vocabulary string, and the catch-all member all refuse —
    including catch-all against catch-all, which is where the tempting
    ``if expected and actual and expected != actual`` shape approves: it treats "nobody
    said" as "no disagreement".

    Never raises for a malformed transaction: a signing gate that throws is a gate that
    gets wrapped in a try/except and bypassed. Every failure path is ``approved=False``,
    including the address-lookup-table refusal — which is checked on BOTH sides, the
    receipt's recorded resolution and the subject's own bytes, because either one alone
    would leave the other free to arrive from somewhere this module did not build.
    """
    attested = getattr(receipt, "message_binding", None)
    strength = getattr(receipt, "binding_strength", None)

    # Before anything else: a receipt whose simulated message loaded accounts from a
    # lookup table attests a set of accounts nobody wrote down. It is checked first so
    # the refusal says WHY rather than falling through to the generic "no binding" —
    # a reason that reads as a missing feature invites someone to go add it.
    if getattr(receipt, "lookup_resolution", None) == "unresolved":
        return SigningVerdict(
            approved=False,
            reason=(
                "receipt simulated a message that loads accounts from an address lookup "
                "table; the bytes do not commit to those accounts"
            ),
            strength=strength if strength in ("structural", "exact") else None,
        )

    if not attested:
        return SigningVerdict(
            approved=False,
            reason="receipt carries no binding — it attests no specific transaction",
        )
    if strength not in ("structural", "exact"):
        return SigningVerdict(
            approved=False, reason="receipt binding strength is missing or unrecognised"
        )
    if require == "exact" and strength != "exact":
        return SigningVerdict(
            approved=False,
            reason="caller requires an exact binding; the receipt is structural only",
            strength=strength,
        )
    # The network, in refusal order. AFFIRMATIVE comparison only: each side must be a
    # named, approvable member and the two must be the same one. Read through `getattr`
    # because `receipt` is an `Any` — a Receipt-SHAPED object from older or third-party
    # code arrives with no `network` at all, and a missing attribute is UNKNOWN, which
    # refuses. Skipping the check there would make the gate strictest on the receipts it
    # built itself and blind to every other one.
    receipt_network = getattr(receipt, "network", None)
    if receipt_network not in APPROVABLE_NETWORKS:
        return SigningVerdict(
            approved=False,
            reason=(
                "receipt does not say which network it simulated against — an "
                "unestablished network approves nothing"
            ),
            strength=strength,
        )
    if expected_network not in APPROVABLE_NETWORKS:
        return SigningVerdict(
            approved=False,
            reason=(
                "caller named no network for this signature — 'unknown' approves "
                "nothing, including against itself"
            ),
            strength=strength,
        )
    if receipt_network != expected_network:
        return SigningVerdict(
            approved=False,
            reason=(
                f"receipt simulated on {receipt_network}; this signature is for "
                f"{expected_network} — a snapshot of one network says nothing "
                "about the other"
            ),
            strength=strength,
        )

    if getattr(receipt, "status", None) != "pass":
        return SigningVerdict(
            approved=False,
            reason=f"receipt did not pass (status={getattr(receipt, 'status', None)})",
            strength=strength,
        )

    try:
        presented = message_binding(tx, encoding=encoding, strength=strength)
    except TxDecodeError as exc:
        return SigningVerdict(approved=False, reason=str(exc), strength=strength)

    if presented != attested:
        return SigningVerdict(
            approved=False,
            reason="this transaction is NOT the one the receipt attests",
            strength=strength,
            presented=presented,
            attested=attested,
        )

    caveat = " (blockhash NOT covered)" if strength == "structural" else ""
    return SigningVerdict(
        approved=True,
        reason=f"binding matches the passing receipt{caveat}",
        strength=strength,
        presented=presented,
        attested=attested,
    )
