"""The read layer: which LIVE instance of a declared account type is the one you mean.

THE GAP, in a live agent's own words. Asked to "contribute 0.1 USDC to the DEATON sale",
it reported that "no tool anywhere in the surface could tell me WHICH ``admin`` pubkey and
``launch_id`` correspond to the human-named DEATON sale... the actual blocker — not a
missing PDA-derivation capability (that part works well), but a missing **name ->
instance-identifier lookup**." It got past it by calling raw ``getProgramAccounts`` and
hex-dumping a 552-byte account by hand to reverse-engineer the struct.

Every piece needed already existed. :mod:`gecko.idl_layout` computes where a field lives
(or refuses); :mod:`gecko.pda` derives; :mod:`gecko.rpc` is the transport. This module is
the assembly, and it adds exactly one idea of its own:

**RE-DERIVATION IS THE PROOF, AND A MEMCMP MATCH IS NOT.** Anyone may create a genuine
account of a declared type with their own admin, so a discriminator match ties an account
to a TYPE and to nothing else. What ties it to the seeds a caller asked about is deriving
the PDA from the decoded values and asserting the result IS the account's own address. A
wrong offset decodes a wrong value, which derives an address that does not match — so a
bad read refutes itself instead of returning a plausible, well-formed, wrong answer. An
account that fails is reported as UNVERIFIED: never dropped, never returned as good.

**NOTHING IS SELECTED FOR THE CALLER.** Every match comes back and the caller chooses.
"There was only one, so it must be the one" is how an agent pays the wrong admin.

**FAIL CLOSED AND LEGIBLY.** No discriminator in the IDL, ``getProgramAccounts`` disabled
on the endpoint, an offset that depends on runtime content, no recipe that could witness
anything — each refuses with the reason it hit. An empty list that could mean any of them
is the failure this module exists to prevent.

**Control plane (invariant #1).** Public chain state is read and returned; nothing is
persisted, and ``dataSlice`` means the account blob is never even fetched — only the byte
prefix the requested fields need. Nothing here signs or broadcasts.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .idl_layout import (
    ANCHOR_DISCRIMINATOR_LEN,
    LayoutError,
    account_discriminator,
    account_size,
    field_layout,
    field_offset,
)
from .account_recipes import (
    INTEGERS,
    PUBKEYS,
    ReadRefusal,
    Refused,
    VerificationRecipe,
    verification_recipe,
)
from .pda import b58_encode, derive_pda
from .rpc import RpcCall, RpcError, default_rpc_call, validate_rpc_url

__all__ = [
    "MAX_TOOL_INSTANCES",
    "READ_ACCOUNTS_TOOL",
    "ReadRefusal",
    "read_accounts",
    "read_accounts_result",
]

#: How many bytes of a Borsh string we are willing to pull. A string has no static width,
#: so the slice is sized by a cap rather than by arithmetic; anything longer is reported
#: as undecoded rather than truncated silently into a value that looks whole.
MAX_STRING_BYTES = 512

#: How many instances the TOOL will render. The library itself is uncapped — a script
#: that wants all 282 UserPositions should get all 282. At the agent boundary the
#: constraint is a context budget, and the only honest way to respect it is to REFUSE
#: with the count rather than return a slice: a truncated list is a selection, and this
#: module's whole contract is that it never selects. The refusal says how many there are
#: and that deriving the one you want is the way through.
MAX_TOOL_INSTANCES = 50

# --- decoding: only the requested fields, only at computed offsets ----------------


@dataclass(frozen=True)
class _Located:
    """Where one requested field sits, and how far into the account it reaches."""

    name: str
    offset: int
    declared: str
    end: int


def _locate(idl: Mapping[str, Any], account_type: str, name: str) -> _Located:
    try:
        layout = field_layout(idl, account_type, name)
        return _Located(
            name,
            layout["offset"],
            str(layout["type"]),
            layout["offset"] + layout["width"],
        )
    except LayoutError as exc:
        # A field whose OWN width is dynamic is still readable when its offset is not:
        # a Borsh string carries its length on the wire. A field sitting BEHIND one is
        # not, and that is the refusal that makes every other offset trustworthy.
        try:
            located = field_offset(idl, account_type, name)
        except LayoutError as inner:
            raise Refused("layout-uncomputable", str(inner)) from inner
        if located["type"] != "string":
            raise Refused("layout-uncomputable", str(exc)) from exc
        offset = int(located["offset"])
        return _Located(name, offset, "string", offset + 4 + MAX_STRING_BYTES)


def _decode(raw: bytes, located: _Located) -> Any:
    """One field's value from the fetched prefix, or ``None`` when the bytes fall short."""
    offset = located.offset
    declared = located.declared
    if declared in PUBKEYS:
        chunk = raw[offset : offset + 32]
        return b58_encode(chunk) if len(chunk) == 32 else None
    if declared in INTEGERS:
        width, signed = INTEGERS[declared]
        chunk = raw[offset : offset + width]
        if len(chunk) < width:
            return None
        return int.from_bytes(chunk, "little", signed=signed)
    if declared == "bool":
        chunk = raw[offset : offset + 1]
        return bool(chunk[0]) if chunk else None
    if declared == "string":
        header = raw[offset : offset + 4]
        if len(header) < 4:
            return None
        length = int.from_bytes(header, "little")
        body = raw[offset + 4 : offset + 4 + length]
        if len(body) < length:
            return None  # longer than MAX_STRING_BYTES — undecoded, never truncated
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def _rpc_refusal(exc: Exception) -> Refused:
    """Classify a transport failure so the caller knows what to do about it.

    ``getProgramAccounts`` is disabled or rate-gated on many public endpoints, and that
    is a different fact from "this program has no accounts of that type" — reporting it
    as an empty list is the specific dishonesty this module refuses.
    """
    text = str(exc)
    lowered = text.lower()
    disabled = any(
        marker in lowered
        for marker in (
            "-32601",
            "method not found",
            "not supported",
            "unsupported",
            "disabled",
            "excluded",
            "410",
        )
    )
    code: ReadRefusal = "rpc-method-unavailable" if disabled else "rpc-failed"
    return Refused(
        code,
        f"getProgramAccounts is unavailable on this endpoint: {text}. This is NOT "
        "evidence that no such accounts exist — point at an RPC that serves "
        "getProgramAccounts and ask again",
    )


def _get_program_accounts(
    call: RpcCall,
    rpc_url: str,
    program_id: str,
    filters: list[dict[str, Any]],
    slice_length: int,
) -> list[dict[str, Any]]:
    config: dict[str, Any] = {
        "encoding": "base64",
        "commitment": "confirmed",
        "dataSlice": {"offset": 0, "length": slice_length},
        "filters": filters,
    }
    try:
        response = call(rpc_url, "getProgramAccounts", [program_id, config])
    except (RpcError, OSError) as exc:
        raise _rpc_refusal(exc) from exc
    result = response.get("result")
    return list(result) if isinstance(result, list) else []


def read_accounts(
    idl: Mapping[str, Any],
    program_id: str,
    account_type: str,
    *,
    fields: Sequence[str] = (),
    rpc_url: str,
    rpc_call: RpcCall | None = None,
) -> dict[str, Any]:
    """Live instances of ``account_type``, each one re-derived from its own contents.

    ``fields`` defaults to exactly the fields the verification recipe needs — the values
    a caller must hold to derive this account's address, which is what the missing lookup
    was for. Ask for more (a human-readable ``name``) and they are decoded too, at
    offsets computed from the IDL; a field whose offset depends on runtime content
    refuses the whole call rather than returning a plausible value.

    Never raises for an answer: a refusal comes back as ``{"refused": True, "code": ...}``.
    """
    try:
        return _read(
            idl,
            program_id,
            account_type,
            fields=fields,
            rpc_url=rpc_url,
            rpc_call=rpc_call or default_rpc_call,
        )
    except Refused as exc:
        return {
            "refused": True,
            "code": exc.code,
            "reason": exc.reason,
            "program_id": program_id,
            "account_type": account_type,
            **exc.extra,
        }


def _read(
    idl: Mapping[str, Any],
    program_id: str,
    account_type: str,
    *,
    fields: Sequence[str],
    rpc_url: str,
    rpc_call: RpcCall,
) -> dict[str, Any]:
    if not program_id or not account_type:
        raise Refused(
            "argument-invalid",
            "read_accounts needs a `program_id` and an `account_type`",
        )
    try:
        validate_rpc_url(rpc_url)
    except RpcError as exc:
        raise Refused("rpc-failed", str(exc)) from exc

    declared = [str(a.get("name")) for a in idl.get("accounts") or () if a.get("name")]
    if account_type not in declared:
        raise Refused(
            "account-type-unknown",
            f"{account_type!r} is not an account type this program declares",
            available=sorted(declared),
        )
    try:
        discriminator = account_discriminator(idl, account_type)
    except LayoutError as exc:
        raise Refused("no-discriminator", str(exc)) from exc

    recipe = verification_recipe(idl, account_type, program_id)

    requested = tuple(dict.fromkeys(str(f) for f in fields)) or recipe.fields
    # the witness fields ride along whether or not they were asked for: without them
    # there is nothing to derive from, and an unwitnessed answer is not one we return.
    decode = tuple(dict.fromkeys(requested + recipe.fields))
    located = [_locate(idl, account_type, name) for name in decode]
    slice_length = max(loc.end for loc in located)

    filters: list[dict[str, Any]] = [
        {"memcmp": {"offset": 0, "bytes": b58_encode(discriminator)}}
    ]
    size: int | None
    try:
        size = account_size(idl, account_type)
    except LayoutError:
        size = None  # a type carrying a string has no static size; say nothing about it
    if size is not None:
        filters.append({"dataSize": size})

    rows = _get_program_accounts(rpc_call, rpc_url, program_id, filters, slice_length)
    size_note = ""
    if size is not None and not rows:
        # A dataSize filter that is one reserved tail out of date returns an EMPTY list
        # that reads exactly like "there are none". Never let that stand unchallenged.
        without_size = [f for f in filters if "dataSize" not in f]
        rows = _get_program_accounts(
            rpc_call, rpc_url, program_id, without_size, slice_length
        )
        if rows:
            size_note = (
                f"the dataSize filter ({size} bytes, computed from the IDL) matched "
                "NOTHING and was dropped; the accounts below are longer than the IDL "
                "declares, so the type carries bytes the IDL does not describe"
            )

    verified: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for row in rows:
        instance = _judge(row, located, requested, recipe, discriminator)
        (verified if instance["verified"] else unverified).append(instance)

    return {
        "refused": False,
        "program_id": program_id,
        "account_type": account_type,
        "verified_by": recipe.to_json(),
        "instances": verified,
        "unverified": unverified,
        "counts": {
            "matched": len(rows),
            "verified": len(verified),
            "unverified": len(unverified),
        },
        "size_note": size_note,
        "note": (
            "every instance that matched is here and NOTHING was selected for you — "
            "the order is the node's and means nothing. Each entry under `instances` "
            "was re-derived from its own decoded seed values back to its own address, "
            "which is what ties it to the seeds you asked about; a discriminator match "
            "alone would not, because anyone may create a genuine account of this type. "
            "Anything under `unverified` did not re-derive: read it, do not use it. "
            "You choose which instance you meant."
        ),
    }


def _judge(
    row: Mapping[str, Any],
    located: Iterable[_Located],
    requested: tuple[str, ...],
    recipe: VerificationRecipe,
    discriminator: bytes,
) -> dict[str, Any]:
    """Decode one account's requested fields and re-derive its address from the seeds."""
    address = str(row.get("pubkey") or "")
    account = row.get("account") if isinstance(row.get("account"), Mapping) else {}
    data = (account or {}).get("data")
    encoded = data[0] if isinstance(data, (list, tuple)) and data else data
    raw = b""
    if isinstance(encoded, str):
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            raw = b""

    values: dict[str, Any] = {}
    undecoded: list[str] = []
    for loc in located:
        value = _decode(raw, loc)
        if value is None:
            undecoded.append(loc.name)
            continue
        values[loc.name] = value

    head = raw[:ANCHOR_DISCRIMINATOR_LEN]
    shown = {name: values[name] for name in requested if name in values}
    base = {
        "address": address,
        "fields": shown,
        "undecoded": undecoded,
        "witnessed_fields": [f for f in recipe.fields if f in values],
    }
    if head != discriminator:
        return {
            **base,
            "verified": False,
            "rederived": None,
            "bump": None,
            "why": (
                "the node returned an account whose leading bytes are not this type's "
                "discriminator — re-derivation was not attempted"
            ),
        }
    missing = [f for f in recipe.fields if f not in values]
    if missing:
        return {
            **base,
            "verified": False,
            "rederived": None,
            "bump": None,
            "why": (
                f"could not decode {', '.join(missing)}, which the seed recipe needs, so "
                "nothing could be re-derived from this account"
            ),
        }
    try:
        derived = derive_pda(recipe.node, values)
    except Exception as exc:  # noqa: BLE001 - a failed derivation is an ANSWER
        return {
            **base,
            "verified": False,
            "rederived": None,
            "bump": None,
            "why": f"re-derivation raised {type(exc).__name__}: {exc}",
        }
    if derived.address != address:
        return {
            **base,
            "verified": False,
            "rederived": derived.address,
            "bump": derived.bump,
            "why": (
                "re-derivation from this account's own decoded seed values produced "
                f"{derived.address}, which is NOT the address the node returned it at. "
                "The decode, the recipe, or the account is wrong — this is reported "
                "rather than dropped, and it must not be used"
            ),
        }
    return {
        **base,
        "verified": True,
        "rederived": derived.address,
        "bump": derived.bump,
        "why": "",
    }


# --- the agent-facing tool ---------------------------------------------------------

READ_ACCOUNTS_TOOL: dict[str, Any] = {
    "name": "read_accounts",
    "description": (
        "Look up the LIVE instances of one of a program's declared account types, and "
        "the values inside them — the step between a human name ('the DEATON sale') and "
        "the identifiers that derive its address (`admin`, `launch_id`). Ask for this "
        "BEFORE `prepare_instruction` whenever a seed value is a fact about an existing "
        "instance rather than something the user told you.\n"
        "Every instance is PROVEN: its seed values are decoded at offsets computed from "
        "the IDL, then derived back to its own address and compared. A discriminator "
        "match alone proves nothing — anyone can create a genuine account of a declared "
        "type with their own admin. Anything under `unverified` failed that check: read "
        "it, never use it.\n"
        "It returns EVERY match and chooses none of them. If several come back, show "
        "them to the user and let them choose; 'there was only one, so it must be the "
        "one' is how an agent pays the wrong person. If it refuses, the code says which "
        "wall it hit (no discriminator in the IDL, getProgramAccounts disabled on the "
        "RPC, an offset that depends on runtime content) — none of which mean 'there "
        "are none'."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "program_id": {
                "type": "string",
                "description": "base58 program address, e.g. from find_start.",
            },
            "account_type": {
                "type": "string",
                "description": (
                    "the account type as the IDL names it, e.g. 'Launch'. Refusing "
                    "names the ones this program declares."
                ),
            },
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "field names to decode. Defaults to exactly the fields needed to "
                    "derive the address. Add a human-readable one (e.g. 'name') when "
                    "you are matching what a person called it."
                ),
            },
        },
        "required": ["program_id", "account_type"],
        "additionalProperties": False,
    },
}


def read_accounts_result(
    arguments: Mapping[str, Any],
    *,
    idl_fetch: Any,
    rpc_url: str,
    rpc_call: RpcCall | None = None,
) -> dict[str, Any]:
    """Tool body: resolve the IDL through the injected catalogue seam, then read.

    ``idl_fetch`` is the same seam ``prepare_instruction`` takes, so the surface holds one
    catalogue client and this path stays falsifiable with no network at all.
    """
    args = arguments or {}
    program_id = str(args.get("program_id") or "").strip()
    account_type = str(args.get("account_type") or "").strip()
    raw_fields = args.get("fields")
    fields = tuple(str(f) for f in raw_fields) if isinstance(raw_fields, list) else ()

    if not program_id or not account_type:
        return {
            "refused": True,
            "code": "argument-invalid",
            "reason": "read_accounts needs a `program_id` and an `account_type`",
        }
    try:
        idl = idl_fetch(program_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "refused": True,
            "code": "argument-invalid",
            "reason": f"the catalogue could not resolve {program_id}: {type(exc).__name__}",
            "program_id": program_id,
        }
    result = read_accounts(
        idl,
        program_id,
        account_type,
        fields=fields,
        rpc_url=rpc_url,
        rpc_call=rpc_call,
    )
    matched = int((result.get("counts") or {}).get("matched", 0))
    if not result.get("refused") and matched > MAX_TOOL_INSTANCES:
        return {
            "refused": True,
            "code": "too-many-instances",
            "reason": (
                f"{matched} live {account_type} accounts exist, more than the "
                f"{MAX_TOOL_INSTANCES} this tool will render. Returning some of them "
                "would be choosing for you, which this tool does not do. If you know "
                "the seed values for the one you want, derive it directly with "
                "`derive_pda`; if you are looking for a named instance, read the type "
                "that carries the name instead."
            ),
            "program_id": program_id,
            "account_type": account_type,
            "counts": result["counts"],
            "verified_by": result["verified_by"],
        }
    return result
