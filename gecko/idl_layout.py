"""Where a field lives inside an Anchor account, computed from the IDL — or refused.

An Anchor IDL 0.1.0 seed entry carries the account TYPE alongside the path:

    {"kind": "account", "path": "launch.admin", "account": "Launch"}

:mod:`gecko.pda_extract` reads ``path`` and discards ``account``, then reports the seed as
"runtime data". Measured over 193 catalogue IDLs: 405 dotted ``account``-kind seeds, **397
(98%) carry the `account` key**, and **384 (95%) name a type with an 8-byte
discriminator**. For those the IDL hands us the memcmp filter and the decode target by
name, with nothing inferred.

This module turns that into ``(discriminator, offset, width)`` — the three facts a
control-plane resolver needs to read the value off-chain, and the three facts an
enumeration needs to find candidate accounts at all.

**Why this is worth computing rather than guessing.** A computed offset can be wrong in a
DETECTABLE way: decode at the wrong offset and you get a wrong seed, which derives an
address that does not match the account you read it from. Re-derivation is therefore a
witness, and it is generated from the IDL rather than hand-written per program — which is
the property ``CLAUDE.md`` says our hand-written witness lacks at catalogue scale. A
guessed offset has no such property; ``gecko/providers/metadao_state.py`` documents one
failing in exactly this shape.

**So the rule is refusal, not best effort.** If any field BEFORE the target is
variable-width — ``string``, ``bytes``, ``vec``, ``option``, or a user type we cannot size
— the offset depends on runtime content and no static answer exists. We raise. A
plausible-looking offset is the failure mode this repo cares about most: the wrong value
it decodes derives a real, correctly formatted, resolvable, WRONG address.

**Control plane.** Everything here is computed from the SURFACE (the IDL). Nothing reads a
chain, and nothing here holds an account's contents.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "ANCHOR_DISCRIMINATOR_LEN",
    "LayoutError",
    "account_discriminator",
    "account_size",
    "field_layout",
    "field_offset",
]

#: Anchor prefixes every account's data with an 8-byte discriminator. An offset that
#: forgets it is off by 8 and decodes the neighbouring field — still a well-formed value,
#: which is precisely why it has to be in the arithmetic rather than in a comment.
ANCHOR_DISCRIMINATOR_LEN = 8

#: Fixed-width primitives, by their Anchor IDL type name. Anything absent here is either
#: variable-width or unknown, and both refuse — there is no "probably 8 bytes" branch.
_FIXED_WIDTHS: Mapping[str, int] = {
    "bool": 1,
    "u8": 1,
    "i8": 1,
    "u16": 2,
    "i16": 2,
    "u32": 4,
    "i32": 4,
    "f32": 4,
    "u64": 8,
    "i64": 8,
    "f64": 8,
    "u128": 16,
    "i128": 16,
    "u256": 32,
    "i256": 32,
    "pubkey": 32,
    "publicKey": 32,  # pre-0.30 spelling
}

#: Named because the refusal message says which one it hit, and "variable" is the word the
#: caller needs to understand why no offset exists.
_VARIABLE = frozenset({"string", "bytes"})


class LayoutError(Exception):
    """A field's position could not be computed from the IDL alone."""


def _struct_fields(idl: Mapping[str, Any], type_name: str) -> list[Mapping[str, Any]]:
    for entry in idl.get("types") or ():
        if entry.get("name") != type_name:
            continue
        shape = entry.get("type") or {}
        if shape.get("kind") != "struct":
            raise LayoutError(
                f"type {type_name!r} is a {shape.get('kind')!r}, not a struct — "
                "no static field layout"
            )
        return list(shape.get("fields") or ())
    raise LayoutError(f"type {type_name!r} is not declared in idl.types")


def _width(declared: Any, idl: Mapping[str, Any], seen: frozenset[str]) -> int:
    """Byte width of one declared type, or raise.

    ``seen`` breaks a recursive ``defined`` cycle. A cycle cannot have a finite width, so
    it refuses rather than recursing — the same class of guard as the graph's cycle report.
    """
    if isinstance(declared, str):
        if declared in _FIXED_WIDTHS:
            return _FIXED_WIDTHS[declared]
        if declared in _VARIABLE:
            raise LayoutError(f"{declared!r} is variable-width")
        raise LayoutError(f"unknown type {declared!r} — refusing to assume a width")

    if isinstance(declared, Mapping):
        if "array" in declared:
            element, count = declared["array"]
            if not isinstance(count, int):
                raise LayoutError(f"array length {count!r} is not a literal")
            return _width(element, idl, seen) * count
        if "vec" in declared:
            raise LayoutError("a vec is variable-width")
        if "option" in declared:
            raise LayoutError(
                "an option is variable-width (its tag byte is not enough)"
            )
        if "defined" in declared:
            inner = declared["defined"]
            name = inner.get("name") if isinstance(inner, Mapping) else inner
            if not isinstance(name, str):
                raise LayoutError(f"malformed defined type {declared!r}")
            if name in seen:
                raise LayoutError(f"type {name!r} is recursive — no finite width")
            return sum(
                _width(field.get("type"), idl, seen | {name})
                for field in _struct_fields(idl, name)
            )

    raise LayoutError(f"unknown type {declared!r} — refusing to assume a width")


def account_discriminator(idl: Mapping[str, Any], account_type: str) -> bytes:
    """The 8-byte discriminator the IDL declares for ``account_type``.

    This is the memcmp filter for enumerating the type's accounts, taken from the IDL
    rather than recomputed from ``sha256("account:<Name>")`` — a declared value and a
    computed one can DISAGREE (a program may have been built with a different Anchor
    version), and the declared one is what the deployed program actually writes.
    """
    for entry in idl.get("accounts") or ():
        if entry.get("name") != account_type:
            continue
        raw = entry.get("discriminator")
        if not raw:
            raise LayoutError(
                f"account {account_type!r} declares no discriminator — this IDL predates "
                "Anchor 0.30 and its accounts cannot be found by memcmp"
            )
        return bytes(raw)
    raise LayoutError(f"account {account_type!r} is not declared in idl.accounts")


def field_offset(
    idl: Mapping[str, Any], account_type: str, field_name: str
) -> dict[str, Any]:
    """``{offset, type}`` for ``field_name`` — WITHOUT requiring its own width.

    Split out of :func:`field_layout` because the two questions are genuinely different.
    "Where does this field start?" is answerable whenever everything BEFORE it is
    fixed-width; "how many bytes is it?" is not answerable for a Borsh ``string``. A
    string is self-describing on the wire (a 4-byte length prefix, then its bytes), so a
    reader that knows the offset can decode it exactly — and refusing the offset because
    the width is dynamic would be over-refusal, which this module calls a wrong answer.

    The refusal that matters is unchanged and lives here: a variable-width field BEFORE
    the target makes the target's offset depend on runtime content, and that raises.
    """
    offset = ANCHOR_DISCRIMINATOR_LEN
    for field in _struct_fields(idl, account_type):
        declared = field.get("type")
        if field.get("name") == field_name:
            return {
                "offset": offset,
                "type": declared if isinstance(declared, str) else "composite",
            }
        # Only what PRECEDES the target moves it. Refusing on a variable-width field that
        # comes after would throw away offsets we can compute exactly, and over-refusal is
        # still a wrong answer.
        try:
            offset += _width(declared, idl, frozenset({account_type}))
        except LayoutError as exc:
            raise LayoutError(
                f"cannot locate {account_type}.{field_name}: the preceding field "
                f"{field.get('name')!r} is variable-width or unknown ({exc}), so every "
                "field after it sits at an offset that depends on runtime content"
            ) from exc
    raise LayoutError(f"field {field_name!r} is not declared on {account_type!r}")


def _declared_type(idl: Mapping[str, Any], account_type: str, field_name: str) -> Any:
    for field in _struct_fields(idl, account_type):
        if field.get("name") == field_name:
            return field.get("type")
    raise LayoutError(f"field {field_name!r} is not declared on {account_type!r}")


def field_layout(
    idl: Mapping[str, Any], account_type: str, field_name: str
) -> dict[str, Any]:
    """``{offset, width, type}`` for ``field_name`` inside ``account_type``.

    The offset counts from the start of the account's DATA, discriminator included, so it
    can be handed straight to a `getAccountInfo` slice. Refuses when the field's own
    width is dynamic — a caller that only needs the position asks :func:`field_offset`.
    """
    located = field_offset(idl, account_type, field_name)
    width = _width(
        _declared_type(idl, account_type, field_name), idl, frozenset({account_type})
    )
    return {"offset": located["offset"], "width": width, "type": located["type"]}


def account_size(idl: Mapping[str, Any], account_type: str) -> int:
    """The exact byte length of ``account_type``, discriminator included, or raise.

    Only a struct whose every field is fixed-width has a static size; anything holding a
    ``string``/``vec``/``option`` does not, and this raises rather than returning a
    plausible number. That distinction is not cosmetic: the size becomes a ``dataSize``
    filter, and a wrong one returns an EMPTY account list that reads exactly like "there
    are none".
    """
    total = ANCHOR_DISCRIMINATOR_LEN
    for field in _struct_fields(idl, account_type):
        total += _width(field.get("type"), idl, frozenset({account_type}))
    return total
