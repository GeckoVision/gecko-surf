"""Our seed model -> ``resolve.pda@1``'s seed vocabulary. The translation that matters.

An Anchor IDL states a seed as ``{kind: "const"|"arg"|"account"|"account_field",
path}`` — a *provenance* vocabulary: where does this value come from. FDL's
``resolve.pda@1`` takes a bare utf8 string or ``{kind: "pubkey"|"string"|"bytes"|
<int>, value}`` — an *encoding* vocabulary: how do these bytes get made. Those are
different axes and the words overlap (``const``, ``account``), so an IDL seed passed
straight through is well-formed FDL that derives the WRONG address. This module maps
one to the other through :mod:`gecko.pda`, which is the only place both facts —
where the value comes from AND how it is encoded — are carried together.

Two things it will not do: guess an unresolved seed, and pick an encoding that is
merely plausible. Both produce an address that derives, compiles and simulates like
a correct one.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping, TypeAlias

from ..pda import (
    ConstantPdaSeedNode,
    OrderedPairPdaSeedNode,
    PdaNode,
    ResolverPdaSeedNode,
    VariablePdaSeedNode,
    b58_encode,
)
from ..program_graph import AccountRef
from .errors import UnprojectableSeedError, UnresolvedSeedProjectionError

__all__ = ["INT_SEED_KINDS", "SeedEntry", "seed_entries"]

#: One entry of a ``resolve.pda@1`` ``seeds`` array: his bare-string form, or the
#: tagged ``{kind, value}`` form.
SeedEntry: TypeAlias = "str | dict[str, str]"

#: The numeric seed kinds ``resolve.pda@1`` accepts. Little-endian at a fixed width,
#: and SIGNEDNESS-BEARING — which is why an integer seed is projected from the IDL's
#: declared arg type (u64 vs i64) and never from our byte width alone.
INT_SEED_KINDS: frozenset[str] = frozenset(
    {"u8", "u16", "u32", "u64", "u128", "i8", "i16", "i32", "i64", "i128"}
)


def seed_entries(
    node: PdaNode,
    account: AccountRef,
    account_values: Mapping[str, str],
    arg_types: Mapping[str, str],
    aliases: Mapping[str, str],
) -> list[SeedEntry]:
    """Translate one PDA recipe into a ``resolve.pda@1`` ``seeds`` array.

    ``account_values`` is the instruction's account slot -> FDL value map (the SAME
    map the build node uses, so a seed and the account slot it mirrors can never
    disagree); ``arg_types`` is the instruction's declared arg types; ``aliases``
    reconciles a seed that names an arg spelled with a leading underscore.
    """
    if not account.resolvable:
        # name the seed, not just the account: "not resolvable" sends a reader back
        # to the IDL, whereas the seed name and its reason say what to go fix.
        unresolved = node.unresolved_seeds
        detail = (
            "unresolved seed(s) "
            + ", ".join(f"{s.name!r} ({s.reason})" for s in unresolved)
            if unresolved
            else "a seed-dependency cycle in this instruction"
        )
        raise UnresolvedSeedProjectionError(
            f"account {account.name!r} cannot be derived — {detail}; Gecko will not "
            "emit a resolve.pda@1 node whose address it cannot stand behind"
        )
    bindings = {b.seed_name: b for b in account.derive_from}
    return [
        _seed_entry(seed, account, bindings, account_values, arg_types, aliases)
        for seed in node.seeds
    ]


def _seed_entry(
    seed: Any,
    account: AccountRef,
    bindings: Mapping[str, Any],
    account_values: Mapping[str, str],
    arg_types: Mapping[str, str],
    aliases: Mapping[str, str],
) -> SeedEntry:
    if isinstance(seed, ConstantPdaSeedNode):
        return _constant_seed(seed)
    if isinstance(seed, ResolverPdaSeedNode):
        raise UnresolvedSeedProjectionError(
            f"PDA {account.name!r} seed {seed.name!r} is unresolved "
            f"({seed.reason}); FDL has no way to say 'this seed is a guess', so the "
            "projection stops here rather than emitting a well-formed wrong address"
        )
    if isinstance(seed, OrderedPairPdaSeedNode):
        raise UnprojectableSeedError(
            f"PDA {account.name!r} has a {seed.select}({seed.left}, {seed.right}) "
            "ordered-pair seed; FDL's expression grammar has no function calls and "
            "resolve.pda@1 has no ordered-pair seed kind, so the byte-sorted pair "
            "cannot be expressed"
        )
    if not isinstance(seed, VariablePdaSeedNode):  # pragma: no cover - model is closed
        raise UnprojectableSeedError(
            f"PDA {account.name!r} carries an unknown seed node {type(seed).__name__}"
        )
    return _variable_seed(seed, account, bindings, account_values, arg_types, aliases)


def _constant_seed(seed: ConstantPdaSeedNode) -> SeedEntry:
    """A fixed seed. utf8 becomes his bare-string form, a 32-byte pubkey becomes
    ``{kind: "pubkey"}``, everything else becomes exact base64 bytes.

    The ``$`` guard is not cosmetic: his interpreter resolves ANY string in a node's
    ``in`` that starts with ``$``, at any depth, so a literal seed whose text starts
    with ``$`` would be read as a reference and silently replaced. Such a seed goes
    out as raw bytes instead.
    """
    if seed.encoding == "utf8":
        text = seed.value.decode("utf-8", "replace")
        if not text.startswith("$"):
            return text
    elif seed.encoding == "pubkey" and len(seed.value) == 32:
        return {"kind": "pubkey", "value": b58_encode(seed.value)}
    return {"kind": "bytes", "value": base64.b64encode(seed.value).decode("ascii")}


def _variable_seed(
    seed: VariablePdaSeedNode,
    account: AccountRef,
    bindings: Mapping[str, Any],
    account_values: Mapping[str, str],
    arg_types: Mapping[str, str],
    aliases: Mapping[str, str],
) -> dict[str, str]:
    reference, arg_name = _seed_reference(
        seed, account, bindings, account_values, aliases
    )
    if seed.encoding == "pubkey":
        return {"kind": "pubkey", "value": reference}
    if seed.encoding == "utf8":
        return {"kind": "string", "value": reference}
    if seed.encoding == "bytes":
        raise UnprojectableSeedError(
            f"PDA {account.name!r} seed {seed.name!r} is raw-bytes-encoded; "
            "resolve.pda@1's bytes kind wants a base64 literal and FLOW_INPUT_TYPES "
            "has no bytes input, so the value cannot reach the node"
        )
    if seed.encoding == "be":
        raise UnprojectableSeedError(
            f"PDA {account.name!r} seed {seed.name!r} is big-endian; every numeric "
            "seed kind resolve.pda@1 accepts is little-endian"
        )
    # le: the width alone is not enough — u64 and i64 are both 8 bytes and encode
    # negatives differently, so the kind comes from the IDL's declared arg type.
    declared = arg_types.get(arg_name or "")
    if declared not in INT_SEED_KINDS:
        raise UnprojectableSeedError(
            f"PDA {account.name!r} seed {seed.name!r} is a {seed.width}-byte "
            "little-endian integer whose signed/unsigned type this graph does not "
            f"carry (declared arg type: {declared!r}); resolve.pda@1 needs an exact "
            "kind and picking one would silently misencode negative values"
        )
    return {"kind": declared, "value": reference}


def _seed_reference(
    seed: VariablePdaSeedNode,
    account: AccountRef,
    bindings: Mapping[str, Any],
    account_values: Mapping[str, str],
    aliases: Mapping[str, str],
) -> tuple[str, str | None]:
    """Where this seed's value comes from, as an FDL value.

    Returns ``(value, arg_name)`` — ``arg_name`` is the instruction arg the seed
    reads, or ``None`` when the seed reads an account.
    """
    binding = bindings.get(seed.name)
    kind = binding.kind if binding is not None else "unresolved"
    bound_to = binding.bound_to if binding is not None else None

    if kind == "account":
        target = bound_to or seed.name
        value = account_values.get(target)
        if value is None:
            raise UnresolvedSeedProjectionError(
                f"PDA {account.name!r} seed {seed.name!r} is bound to account "
                f"{target!r}, which this instruction does not declare"
            )
        return value, None
    if kind == "argument":
        arg = bound_to or seed.name
        return f"$inputs.{arg}", arg
    alias = aliases.get(seed.name)
    if alias is not None:
        return f"$inputs.{alias}", alias
    raise UnresolvedSeedProjectionError(
        f"PDA {account.name!r} seed {seed.name!r} binds to nothing this instruction "
        "supplies — it is neither one of its accounts nor one of its args. Gecko "
        "will not declare it as a flow input: an input the caller has to invent is "
        "a wrong PDA that derives, compiles and simulates against the wrong account"
    )
