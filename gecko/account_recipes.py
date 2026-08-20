"""IDL -> the seed recipe that lets an account PROVE which account it is.

Split out of :mod:`gecko.read_accounts` because it is a different job done at a
different time. This module reads a SURFACE and touches no chain; ``read_accounts``
reads a chain and touches no IDL beyond what this hands it.

THE SHAPE IT LOOKS FOR, and why it is the interesting one. An Anchor seed entry can name
the struct a value is read from::

    {"kind": "account", "path": "launch.admin", "account": "Launch"}

A ``launch`` seeded on its own ``admin`` and ``launch_id`` cannot be derived by a caller
who does not already hold them — which is exactly the dead end a catalogue's
``derive_pda`` reports, correctly. Read in the other direction the same recipe is a
WITNESS: decode those fields off a candidate account, derive, and the result either is
that account's address or the read was wrong. The recipe is generated from the IDL rather
than hand-written per program, which is the property ``CLAUDE.md`` says our hand-written
witness lacks at catalogue scale.

MEASURED, so nobody reads more into it than is there. Across four catalogue programs (29
declared account types) this finds a witness for 5 and identifies 5 more as singletons
whose one address is simply derivable; the remaining 19 refuse, because their seeds come
from instruction arguments or from accounts they do not store. On jurassic_fi it finds one
for all three. A refusal here is the common case and it is the honest one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .pda import (
    ConstantPdaSeedNode,
    PdaNode,
    PdaSeed,
    VariablePdaSeedNode,
    b58_encode,
    derive_pda,
)

__all__ = [
    "ReadRefusal",
    "Refused",
    "SeedBasis",
    "SeedBinding",
    "VerificationRecipe",
    "verification_recipe",
]

#: byte width of every integer Borsh writes little-endian, and whether it is signed.
#: Shared with the decoder in :mod:`gecko.read_accounts` through this module so a seed's
#: encoding and the decode of the same field can never disagree about a width.
INTEGERS: Mapping[str, tuple[int, bool]] = {
    "u8": (1, False),
    "i8": (1, True),
    "u16": (2, False),
    "i16": (2, True),
    "u32": (4, False),
    "i32": (4, True),
    "u64": (8, False),
    "i64": (8, True),
    "u128": (16, False),
    "i128": (16, True),
}
PUBKEYS = frozenset({"pubkey", "publicKey"})


#: Which refusal a caller hit. Closed, and each member names a DIFFERENT thing to do
#: about it — which is the whole reason they are not one "no results".
ReadRefusal = Literal[
    "argument-invalid",
    "account-type-unknown",
    "no-discriminator",
    "no-verification-recipe",
    "ambiguous-verification-recipe",
    "singleton-account",
    "layout-uncomputable",
    "rpc-method-unavailable",
    "rpc-failed",
    "too-many-instances",
]

#: Where one seed of the verification recipe came from. ``idl-stated`` is the IDL's own
#: word (an Anchor seed entry carries ``{"kind": "account", "path": "launch.admin",
#: "account": "Launch"}`` — it NAMES the struct the value is read from).
#: ``field-name-match`` is INFERRED: the seed is a plain account path and the struct has
#: a pubkey field of that same name. The inference is safe only because it still has to
#: survive re-derivation, and it is labelled so nobody reads it as the IDL's word.
SeedBasis = Literal["constant", "idl-stated", "field-name-match"]


class Refused(Exception):
    """Internal control flow: a refusal carrying its code, rendered by the entry point.

    Messages carry only public data — program ids, type names, field names, byte offsets
    and the RPC's own error text. On-chain reads have no secret material (invariant #1).
    """

    def __init__(self, code: ReadRefusal, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.extra = extra


@dataclass(frozen=True)
class SeedBinding:
    """One seed of the recipe that re-derives an account from its own contents."""

    position: int
    #: the field the value is decoded from — ``None`` for a constant seed
    field: str | None
    basis: SeedBasis

    def to_json(self) -> dict[str, Any]:
        return {"position": self.position, "field": self.field, "basis": self.basis}


@dataclass(frozen=True)
class VerificationRecipe:
    """The witness: seeds expressed entirely in terms of the account's OWN fields.

    This is the shape that makes a self-seeded PDA — the one a catalogue's ``derive_pda``
    dead-ends on, because computing the address needs what is inside it — readable in the
    other direction. Read the account, decode the seeds, derive, compare.
    """

    account_type: str
    #: the instruction slot the recipe was read from, for provenance
    instruction: str
    slot: str
    program_id: str
    seeds: tuple[SeedBinding, ...]
    node: PdaNode

    @property
    def fields(self) -> tuple[str, ...]:
        """The fields the derivation needs, in seed order, deduplicated."""
        names = [s.field for s in self.seeds if s.field]
        return tuple(dict.fromkeys(names))

    def to_json(self) -> dict[str, Any]:
        return {
            "account_type": self.account_type,
            "from_instruction": self.instruction,
            "slot": self.slot,
            "program_id": self.program_id,
            "seeds": [s.to_json() for s in self.seeds],
        }


def _snake(name: str) -> str:
    """``UserPosition`` -> ``user_position`` — the Anchor convention for a slot name."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _struct_field_types(idl: Mapping[str, Any], account_type: str) -> dict[str, Any]:
    for entry in idl.get("types") or ():
        if entry.get("name") != account_type:
            continue
        shape = entry.get("type") or {}
        if shape.get("kind") != "struct":
            return {}
        return {
            str(f.get("name")): f.get("type")
            for f in (shape.get("fields") or ())
            if f.get("name")
        }
    return {}


def _seed_from_field(name: str, declared: Any) -> PdaSeed | None:
    """A field's declared type -> the seed encoding that reproduces its on-chain bytes.

    ``None`` when the type is one whose seed bytes we would have to guess. The width is
    read, never defaulted: a ``u64`` read as ``u8`` derives a different, perfectly valid,
    wrong address, which is the defect this repo has already paid for once.
    """
    # An Anchor field type is not always a string: `{"array": [...]}`, `{"defined": {...}}`,
    # `{"option": ...}` and `{"vec": ...}` are dicts, and a dict is unhashable — so the
    # membership test below raised `TypeError: unhashable type: 'dict'` before any refusal
    # could be reached. 5 of 531 catalogue account types crashed this way.
    #
    # A crash is not a refusal: it carries no reason, it cannot be counted, and it escapes
    # as an exception type nothing downstream is watching for. A composite is simply a type
    # whose seed bytes we would have to guess, which is what `None` already means here.
    if not isinstance(declared, str):
        return None
    if declared in PUBKEYS:
        return VariablePdaSeedNode(name, source="account", encoding="pubkey")
    if isinstance(declared, str) and declared in INTEGERS:
        width, _signed = INTEGERS[declared]
        return VariablePdaSeedNode(name, source="account", encoding="le", width=width)
    if declared == "string":
        return VariablePdaSeedNode(name, source="account", encoding="utf8")
    return None


def _constant_seed(seed: Mapping[str, Any]) -> PdaSeed | None:
    raw = seed.get("value")
    if isinstance(raw, (list, tuple)) and all(isinstance(b, int) for b in raw):
        return ConstantPdaSeedNode(bytes(raw), "bytes")
    if isinstance(raw, str):
        return ConstantPdaSeedNode(raw.encode(), "utf8")
    return None


def _recipe_program(pda: Mapping[str, Any], default: str) -> str | None:
    """The program the PDA derives under. ``None`` when the IDL cannot pin it."""
    program = pda.get("program")
    if not isinstance(program, Mapping):
        return default
    if program.get("kind") == "const":
        value = bytes(program.get("value") or ())
        return b58_encode(value) if len(value) == 32 else None
    return None  # a program taken from an account slot: we hold no such account here


def _candidate_recipe(
    instruction: str,
    slot: Mapping[str, Any],
    account_type: str,
    field_types: Mapping[str, Any],
    default_program: str,
) -> VerificationRecipe | None:
    """One instruction slot -> a recipe expressed in the account's own fields, or ``None``.

    Rejects rather than approximates. A seed taken from an ARGUMENT, from another
    account, or from a field whose bytes we cannot reproduce leaves nothing to witness
    with, and half a witness is no witness.
    """
    pda = slot.get("pda")
    if not isinstance(pda, Mapping):
        return None
    slot_name = str(slot.get("name") or "")
    stated = any(
        s.get("account") == account_type
        for s in pda.get("seeds") or ()
        if isinstance(s, Mapping)
    )
    if not stated and slot_name != _snake(account_type):
        return None

    program = _recipe_program(pda, default_program)
    if program is None:
        return None

    seeds: list[PdaSeed] = []
    bindings: list[SeedBinding] = []
    for position, seed in enumerate(pda.get("seeds") or ()):
        if not isinstance(seed, Mapping):
            return None
        kind = seed.get("kind")
        if kind == "const":
            node = _constant_seed(seed)
            if node is None:
                return None
            seeds.append(node)
            bindings.append(SeedBinding(position, None, "constant"))
            continue
        if kind != "account":
            return None  # an argument seed is the caller's value, not the account's
        path = str(seed.get("path") or "")
        if "." in path:
            head, _, field = path.partition(".")
            if seed.get("account") != account_type or "." in field:
                return None
            basis: SeedBasis = "idl-stated"
        else:
            # A plain account path is an ADDRESS. Binding it to a same-named pubkey field
            # is an inference, and it is labelled one — re-derivation is what makes it
            # safe to try at all.
            field = path
            if field_types.get(field) not in PUBKEYS:
                return None
            basis = "field-name-match"
        node = _seed_from_field(field, field_types.get(field))
        if node is None:
            return None
        seeds.append(node)
        bindings.append(SeedBinding(position, field, basis))

    return VerificationRecipe(
        account_type=account_type,
        instruction=instruction,
        slot=slot_name,
        program_id=program,
        seeds=tuple(bindings),
        node=PdaNode(
            name=slot_name or account_type, seeds=tuple(seeds), program_id=program
        ),
    )


def verification_recipe(
    idl: Mapping[str, Any], account_type: str, program_id: str
) -> VerificationRecipe:
    """The one recipe that re-derives ``account_type`` from its own fields, or refuse.

    Several instructions usually declare the SAME recipe (jurassic_fi's five instructions
    that take a ``launch`` all seed it identically); those collapse to one. Two genuinely
    different recipes for one type refuse: picking between them would be a guess, and the
    refusal names the instructions so a human can look.
    """
    field_types = _struct_field_types(idl, account_type)
    found: dict[tuple[Any, ...], VerificationRecipe] = {}
    singletons: dict[tuple[Any, ...], VerificationRecipe] = {}
    for instruction in idl.get("instructions") or ():
        if not isinstance(instruction, Mapping):
            continue
        name = str(instruction.get("name") or "")
        for slot in instruction.get("accounts") or ():
            if not isinstance(slot, Mapping):
                continue
            recipe = _candidate_recipe(
                name, slot, account_type, field_types, program_id
            )
            if recipe is None:
                continue
            key = (recipe.program_id, tuple(recipe.node.seeds))
            # A recipe with no field seeds is a SINGLETON — constants alone fix its one
            # address. It is kept apart because it answers a different question, and
            # answering it as "no recipe" sends a caller looking for a lookup that
            # cannot exist rather than at the one address that does.
            (singletons if not recipe.fields else found).setdefault(key, recipe)

    if not found and singletons:
        singleton = next(iter(singletons.values()))
        head = (
            f"{account_type} is a SINGLETON: every seed of it is a constant, so the "
            "program has exactly one and enumerating is the wrong question."
        )
        try:
            derived = derive_pda(singleton.node, {})
        except Exception as exc:  # noqa: BLE001 - an underivable singleton is still one
            raise Refused(
                "singleton-account",
                f"{head} Deriving it here raised {type(exc).__name__}; the recipe "
                f"{singleton.instruction!r} declares is the one to use",
            ) from exc
        raise Refused(
            "singleton-account",
            f"{head} Its address is {derived.address} (bump {derived.bump}), derived "
            f"from the recipe {singleton.instruction!r} declares — no read needed and "
            "nothing to choose between",
            address=derived.address,
            bump=derived.bump,
        )
    if not found:
        raise Refused(
            "no-verification-recipe",
            f"no instruction of this program seeds a {account_type} entirely from values "
            f"{account_type} itself stores at computable offsets. A seed taken from an "
            "instruction ARGUMENT, from a different account, or from a field sitting "
            "behind a variable-width one cannot be recovered by reading the account, so "
            "there is nothing to witness with — and a discriminator match is not proof, "
            "because anyone can create a genuine account of a declared type. Nothing is "
            "returned rather than a list of addresses no one can check",
        )
    if len(found) > 1:
        raise Refused(
            "ambiguous-verification-recipe",
            f"{len(found)} DIFFERENT seed recipes address {account_type} "
            f"({', '.join(sorted(r.instruction for r in found.values()))}); choosing one "
            "would be a guess, and the wrong one reports every real account as unverified",
        )
    return next(iter(found.values()))
