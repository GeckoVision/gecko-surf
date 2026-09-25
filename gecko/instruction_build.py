"""An IDL instruction -> the exact bytes a program reads, encoded HERE, offline.

WHY THIS IS IN THE ENGINE AND NOT BESIDE A PROGRAM. On 2026-09-25 the hosted builder a
purchase depended on answered ``HTTP 500`` to five of six identical requests — its own
upstream RPC was rate-limiting a blockhash Gecko discards anyway. The fix is to build the
instruction locally, and the wrong way to ship that fix is one hand-rolled encoder per
program: that is ``providers/`` growing a second engine, and the second copy is the one
that drifts. So the encoder takes **an IDL instruction as data** and knows nothing about
which program it came from. Adding a program's local build touches that program's config,
never this file.

WHAT IT IS BUILT ON, rather than beside:

* :func:`gecko.artifact.instruction_encoding` already yields the discriminator with its
  PROVENANCE — ``verified`` when the IDL's eight bytes and ``sha256("global:<name>")[:8]``
  agree, ``disagree`` when they do not. A disagreement is refused here rather than
  resolved: a surface whose own two answers differ is exactly the case where picking one
  produces a well-formed call against the wrong entry point.
* :func:`gecko.landing.assemble_unsigned_tx` already compiles the message (legacy, or v0
  when lookup tables are present) and leaves the signature slots zeroed.

IT REFUSES RATHER THAN GUESSES. Every arg type it cannot encode raises
:class:`InstructionEncodeError` NAMING the type. There is no default branch, no zero pad,
no "probably a u64". The known open gap is the ``defined`` types — pump.fun's ``buy``
takes ``track_volume: OptionBool``, a program-declared struct whose layout is not in the
instruction's own args — and that raises by name instead of emitting bytes. A refusal a
caller can read is worth more than a transaction that lands somewhere unintended.

CONTROL PLANE ONLY: an IDL and the caller's own arguments in, bytes out. Nothing is
persisted, nothing is fetched, no key is touched, and the result is UNSIGNED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .artifact import instruction_encoding
from .landing import assemble_unsigned_tx
from .simulate import BuiltTx

__all__ = [
    "MAX_INSTRUCTION_DATA",
    "AccountSlot",
    "InstructionEncodeError",
    "build_unsigned_instruction",
    "encode_instruction_data",
    "instruction_accounts",
    "make_instruction",
]

#: The hard bound on instruction data, and it is a FACT rather than a policy: a Solana
#: packet is 1232 bytes, so data past it cannot ride in any transaction. Used as the cap
#: on variable-length args (a ``String``/``Vec`` is attacker-reachable in principle) so an
#: over-long value is named as such here instead of failing far away as a serialisation
#: error nobody can trace back to its field.
MAX_INSTRUCTION_DATA = 1232

#: Unsigned Borsh scalars: name -> byte width.
_UNSIGNED: dict[str, int] = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16}
#: Signed Borsh scalars: name -> byte width.
_SIGNED: dict[str, int] = {"i8": 1, "i16": 2, "i32": 4, "i64": 8, "i128": 16}
#: Spelled both ways across the two IDL generations; both mean 32 raw bytes.
_PUBKEY = ("pubkey", "publicKey")


class InstructionEncodeError(ValueError):
    """An instruction that cannot be encoded — a type we refuse, a value out of range,
    an account nobody supplied. Never a network condition, and never resolved by guessing.
    """


@dataclass(frozen=True)
class AccountSlot:
    """One account slot of an instruction, as the IDL declares it.

    ``address`` is the address the IDL PINS (the program's own word — the token program,
    the system program). It is never asked of a caller, because asking is how a flow ends
    up parameterising a program id.
    """

    name: str
    is_signer: bool
    is_writable: bool
    address: str | None = None


# --- the arg layout -------------------------------------------------------------------


def _type_label(declared: Any) -> str:
    """The declared type as a human would say it — used only in refusals."""
    if isinstance(declared, str):
        return declared
    if isinstance(declared, Mapping):
        for key in ("defined", "option", "vec", "array", "coption"):
            if key in declared:
                inner = declared[key]
                if key == "defined":
                    if isinstance(inner, Mapping):
                        return str(inner.get("name", "defined"))
                    return str(inner)
                return f"{key}<{_type_label(inner)}>"
    return repr(declared)


def _encode_uint(value: Any, width: int, *, field: str, label: str) -> bytes:
    # bool is an int in Python; admitting it here would silently encode True as 1 under a
    # numeric field the caller never filled.
    if not isinstance(value, int) or isinstance(value, bool):
        raise InstructionEncodeError(
            f"`{field}` is declared {label} and must be an int, got "
            f"{type(value).__name__}"
        )
    if not 0 <= value < (1 << (width * 8)):
        raise InstructionEncodeError(f"`{field}` = {value} does not fit a {label}")
    return value.to_bytes(width, "little")


def _encode_int(value: Any, width: int, *, field: str, label: str) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InstructionEncodeError(
            f"`{field}` is declared {label} and must be an int, got "
            f"{type(value).__name__}"
        )
    bound = 1 << (width * 8 - 1)
    if not -bound <= value < bound:
        raise InstructionEncodeError(f"`{field}` = {value} does not fit a {label}")
    return value.to_bytes(width, "little", signed=True)


def _encode_bytes_like(raw: bytes, *, field: str) -> bytes:
    if len(raw) > MAX_INSTRUCTION_DATA:
        raise InstructionEncodeError(
            f"`{field}` is {len(raw)} bytes; no transaction carries more than "
            f"{MAX_INSTRUCTION_DATA}, so this cannot land"
        )
    return len(raw).to_bytes(4, "little") + raw


def _encode_pubkey(value: Any, *, field: str) -> bytes:
    from solders.pubkey import Pubkey

    if not isinstance(value, str) or not value:
        raise InstructionEncodeError(
            f"`{field}` is a pubkey and must be a base58 string"
        )
    try:
        return bytes(Pubkey.from_string(value))
    except Exception as exc:  # noqa: BLE001 - solders raises its own ValueError subclass
        raise InstructionEncodeError(f"`{field}` is not a base58 pubkey") from exc


def _encode_value(declared: Any, value: Any, *, field: str) -> bytes:
    """One Borsh value. Every unhandled type RAISES, naming itself."""
    if isinstance(declared, str):
        if declared == "bool":
            if not isinstance(value, bool):
                raise InstructionEncodeError(
                    f"`{field}` is declared bool and must be a bool, got "
                    f"{type(value).__name__}"
                )
            return b"\x01" if value else b"\x00"
        if declared in _UNSIGNED:
            return _encode_uint(value, _UNSIGNED[declared], field=field, label=declared)
        if declared in _SIGNED:
            return _encode_int(value, _SIGNED[declared], field=field, label=declared)
        if declared in ("string", "String"):
            if not isinstance(value, str):
                raise InstructionEncodeError(
                    f"`{field}` is declared string and must be a str, got "
                    f"{type(value).__name__}"
                )
            return _encode_bytes_like(value.encode("utf-8"), field=field)
        if declared in _PUBKEY:
            return _encode_pubkey(value, field=field)
        if declared == "bytes":
            if not isinstance(value, (bytes, bytearray)):
                raise InstructionEncodeError(
                    f"`{field}` is declared bytes and must be bytes, got "
                    f"{type(value).__name__}"
                )
            return _encode_bytes_like(bytes(value), field=field)
        raise InstructionEncodeError(
            f"`{field}` is declared `{declared}`, a type this encoder does not encode. "
            "Refusing rather than guessing a layout."
        )

    if isinstance(declared, Mapping):
        if "option" in declared:
            if value is None:
                return b"\x00"
            return b"\x01" + _encode_value(declared["option"], value, field=field)
        if "vec" in declared:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise InstructionEncodeError(
                    f"`{field}` is declared a vec and must be a list, got "
                    f"{type(value).__name__}"
                )
            body = b"".join(
                _encode_value(declared["vec"], item, field=f"{field}[{i}]")
                for i, item in enumerate(value)
            )
            if len(body) > MAX_INSTRUCTION_DATA:
                raise InstructionEncodeError(
                    f"`{field}` encodes to {len(body)} bytes; no transaction carries "
                    f"more than {MAX_INSTRUCTION_DATA}"
                )
            return len(value).to_bytes(4, "little") + body
        if "array" in declared:
            spec = declared["array"]
            if not (isinstance(spec, Sequence) and len(spec) == 2):
                raise InstructionEncodeError(
                    f"`{field}` declares a malformed array type: {declared!r}"
                )
            inner, count = spec[0], spec[1]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise InstructionEncodeError(
                    f"`{field}` declares an array of non-literal length "
                    f"({count!r}); refusing to guess how many elements it holds"
                )
            if not isinstance(value, Sequence) or isinstance(value, str):
                raise InstructionEncodeError(
                    f"`{field}` is declared an array and must be a sequence, got "
                    f"{type(value).__name__}"
                )
            if len(value) != count:
                raise InstructionEncodeError(
                    f"`{field}` is declared [{_type_label(inner)}; {count}] but "
                    f"{len(value)} elements were given — a fixed array is not padded here"
                )
            return b"".join(
                _encode_value(inner, item, field=f"{field}[{i}]")
                for i, item in enumerate(value)
            )

    raise InstructionEncodeError(
        f"`{field}` is declared `{_type_label(declared)}`, a type this encoder does not "
        "encode. Refusing rather than guessing a layout."
    )


def _discriminator(idl_instruction: Mapping[str, Any]) -> bytes:
    encoding = instruction_encoding(idl_instruction)
    source = encoding.get("discriminator_source")
    name = str(idl_instruction.get("name", ""))
    if source == "disagree":
        raise InstructionEncodeError(
            f"the IDL's discriminator for `{name}` is not "
            f'sha256("global:{name}")[:8]; the surface disagrees with itself and either '
            "choice may be the wrong entry point, so neither is used"
        )
    return bytes(encoding["discriminator"])


def encode_instruction_data(
    idl_instruction: Mapping[str, Any], args: Mapping[str, Any]
) -> bytes:
    """The instruction data: the 8-byte discriminator, then each arg in IDL order (Borsh).

    ``idl_instruction`` is the IDL's OWN shape — ``{"name", "discriminator", "args": [...]}``
    — so the caller supplies data, not code. Raises :class:`InstructionEncodeError` for a
    missing arg, a value out of range, or a declared type this encoder will not encode.
    Extra keys in ``args`` are ignored: the IDL, not the caller, decides what is on the
    wire.
    """
    data = bytearray(_discriminator(idl_instruction))
    declared_args = idl_instruction.get("args") or []
    if not isinstance(declared_args, Sequence):
        raise InstructionEncodeError(
            f"the IDL for `{idl_instruction.get('name')}` declares a malformed args list"
        )
    for arg in declared_args:
        if not isinstance(arg, Mapping) or not arg.get("name"):
            raise InstructionEncodeError(
                f"the IDL for `{idl_instruction.get('name')}` declares an unnamed "
                "argument; refusing to encode a field nobody can fill"
            )
        field = str(arg["name"])
        if field not in args:
            raise InstructionEncodeError(
                f"no value was given for the `{field}` argument"
            )
        data += _encode_value(arg.get("type"), args[field], field=field)
    if len(data) > MAX_INSTRUCTION_DATA:
        raise InstructionEncodeError(
            f"the encoded instruction is {len(data)} bytes; no transaction carries more "
            f"than {MAX_INSTRUCTION_DATA}"
        )
    return bytes(data)


# --- the account list -----------------------------------------------------------------


def instruction_accounts(idl_instruction: Mapping[str, Any]) -> tuple[AccountSlot, ...]:
    """The IDL's account slots, IN ORDER, with their signer/writable flags.

    ORDER IS PART OF THE ABI. The program reads its accounts positionally, so a reordered
    list is a different call that may still simulate and still land. It is taken from the
    IDL and never sorted, never de-duplicated, never reordered to match a caller's map.
    """
    declared = idl_instruction.get("accounts") or []
    if not isinstance(declared, Sequence):
        raise InstructionEncodeError(
            f"the IDL for `{idl_instruction.get('name')}` declares a malformed accounts "
            "list"
        )
    slots: list[AccountSlot] = []
    for entry in declared:
        if not isinstance(entry, Mapping) or not entry.get("name"):
            raise InstructionEncodeError(
                f"the IDL for `{idl_instruction.get('name')}` declares an unnamed "
                "account slot; refusing to build a positional list with a hole in it"
            )
        if "accounts" in entry:
            # A nested account GROUP (Anchor composite). Flattening it would invent a
            # position for each member, and the position is the ABI.
            raise InstructionEncodeError(
                f"`{entry['name']}` is a nested account group, which this encoder does "
                "not flatten — the flattened order would be a guess"
            )
        pinned = entry.get("address")
        slots.append(
            AccountSlot(
                name=str(entry["name"]),
                is_signer=bool(entry.get("signer") or entry.get("isSigner")),
                is_writable=bool(entry.get("writable") or entry.get("isMut")),
                address=str(pinned) if isinstance(pinned, str) and pinned else None,
            )
        )
    if not slots:
        raise InstructionEncodeError(
            f"the IDL for `{idl_instruction.get('name')}` declares no accounts"
        )
    return tuple(slots)


def make_instruction(
    idl_instruction: Mapping[str, Any],
    *,
    program_id: str,
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
) -> Any:
    """A solders ``Instruction`` for this IDL instruction. Never signs, never sends.

    ``accounts`` maps the IDL's account NAMES to base58 addresses — the map a deriver
    produces. A slot the IDL pins falls back to the pinned address; anything else missing
    is a refusal naming the slot, because a transaction carrying a WRONG account is worse
    than one that is never built: it is well-formed, it may land, and nothing downstream
    catches it.
    """
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    metas = []
    for slot in instruction_accounts(idl_instruction):
        supplied = accounts.get(slot.name)
        address = supplied if isinstance(supplied, str) and supplied else slot.address
        if not address:
            raise InstructionEncodeError(f"plan is missing the `{slot.name}` account")
        try:
            pubkey = Pubkey.from_string(address)
        except Exception as exc:  # noqa: BLE001 - solders raises a ValueError subclass
            raise InstructionEncodeError(
                f"`{slot.name}` is not a base58 pubkey"
            ) from exc
        metas.append(
            AccountMeta(pubkey, is_signer=slot.is_signer, is_writable=slot.is_writable)
        )

    try:
        program = Pubkey.from_string(program_id)
    except Exception as exc:  # noqa: BLE001
        raise InstructionEncodeError("program_id is not a base58 pubkey") from exc
    return Instruction(program, encode_instruction_data(idl_instruction, args), metas)


def build_unsigned_instruction(
    idl_instruction: Mapping[str, Any],
    *,
    program_id: str,
    accounts: Mapping[str, str],
    args: Mapping[str, Any],
    fee_payer: str,
    blockhash: str | None = None,
) -> BuiltTx:
    """One instruction, compiled into an UNSIGNED transaction — the whole local build.

    The blockhash defaults to the all-zero placeholder, exactly as the hosted builder's
    was treated: the caller re-stamps a fresh one from the node it simulates against
    (:func:`gecko.txbind.blockhash_offset`). With a ``fee_payer`` other than the actor the
    message carries two required signatures, payer first, which is what the relay path
    expects.
    """
    if not isinstance(fee_payer, str) or not fee_payer:
        raise InstructionEncodeError("plan must name a fee payer")
    instruction = make_instruction(
        idl_instruction, program_id=program_id, accounts=accounts, args=args
    )
    return assemble_unsigned_tx([instruction], fee_payer, blockhash=blockhash)
