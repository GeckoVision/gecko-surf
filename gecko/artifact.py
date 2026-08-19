"""The per-program artifact — what a catalogue stores beside an IDL so agents can call it.

`docs/specs/2026-08-18-orquestra-artifact.md` argues for this shape; this builds it.

WHAT IT ADDS OVER `ProgramGraph.to_json()`, which already carries the recipes, the widths,
the provenance and the derivation order:

**`needs`, per instruction.** The list of values a caller must go and fetch before the call
can be built — each with the account it seeds and why this instruction cannot supply it. A
live agent session is the reason: given a program whose root PDA seeds on fields of itself,
the surface said "unresolvable" and the agent stopped. "Unresolvable" is honest and it is
not actionable. "Read `admin` off the `launch` account" is the same fact, and a caller can
act on it.

**A header that states the artifact's own limits**, so a reader does not have to infer
them: which IDL it was generated from, and that a `recovered` recipe is a reconstruction
rather than the program's word.

CONTROL PLANE ONLY. Recipes, provenance and shapes. No response payloads, no balances, no
user data, no secrets — the same promise that makes ingesting a surface unilaterally
defensible in the first place.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .program_graph import ProgramGraph, build_program_graph

__all__ = [
    "ARTIFACT_VERSION",
    "instruction_encoding",
    "build_artifact",
    "instruction_needs",
]

#: Bumped only when the SHAPE changes in a way a consumer must notice. A reader that
#: pins this and finds a different value should re-read rather than guess.
ARTIFACT_VERSION = "1"


def _seed_label(seed: Mapping[str, Any]) -> str:
    """A seed rendered the way a person would say it, width included.

    The width is not decoration. The same value at u8, u16 and u32 derives three different
    addresses that are all valid, so a recipe that omits it is a recipe a caller can follow
    exactly and still get wrong.
    """
    if seed.get("kind") == "const":
        text = seed.get("utf8")
        return f'"{text}"' if text else f"const:{seed.get('bytes_b64', '')[:12]}"
    name = seed.get("name", "?")
    encoding = seed.get("encoding")
    if encoding == "pubkey":
        return f"{name}: pubkey"
    if encoding in ("le", "be") and seed.get("width"):
        return f"{name}: u{int(seed['width']) * 8} {encoding.upper()}"
    if encoding:
        return f"{name}: {encoding}"
    return f"{name}: UNKNOWN"


def instruction_needs(
    instruction: Mapping[str, Any], pdas: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The values a caller must fetch before this instruction can be built.

    A PDA seed binds to an account or an argument OF THIS INSTRUCTION, or it binds to
    nothing — and "nothing" is the interesting case. It means the value exists somewhere
    the caller has to go and read, most often inside the very account being derived.

    Returned rather than raised, and named rather than counted, because a refusal a caller
    cannot act on is a shrug. Each entry says which account wants the value and what the
    value is, so the next step is obvious.
    """
    needs: dict[str, dict[str, Any]] = {}
    for account in instruction.get("accounts", []):
        if not account.get("is_pda"):
            continue
        recipe = pdas.get(account["name"], {})
        for binding in account.get("derive_from", []):
            if binding.get("kind") != "unresolved":
                continue
            seed = binding.get("seed", "?")
            entry = needs.setdefault(
                seed,
                {
                    "value": seed,
                    "seeds": [],
                    "encoding": binding.get("encoding") or None,
                    "why": "",
                },
            )
            if account["name"] not in entry["seeds"]:
                entry["seeds"].append(account["name"])
            # WHERE the value lives decides what a caller does next, and the two dotted
            # cases are opposites. `launch.admin` is a field of an ACCOUNT — go read it.
            # `params.launch_id` is a field of an ARGUMENT STRUCT the caller constructs,
            # so they already hold it; telling them to read it on-chain would send them
            # after a value that has no account.
            source = next(
                (
                    seed_def.get("source")
                    for seed_def in recipe.get("seeds", [])
                    if seed_def.get("name") == seed
                ),
                None,
            )
            head = seed.partition(".")[0]
            # Does THIS instruction actually take that argument? The recipe may have been
            # imported from a sibling that does, and `source` describes the sibling's
            # declaration, not this call's. Telling a caller they already hold a value
            # they were never given invites them to invent one — and an invented seed
            # derives a real, correctly formatted, resolvable, WRONG address.
            declares_head = any(
                arg.get("name") == head for arg in instruction.get("args", []) or ()
            )
            if "." in seed and source == "account":
                entry["why"] = (
                    f"a field of the `{head}` account — read it on-chain and pass it in"
                )
            elif "." in seed and source == "argument" and declares_head:
                entry["why"] = (
                    f"a field of the `{head}` argument struct — you build it, so pass "
                    f"`{seed}` directly"
                )
            elif "." in seed and source == "argument":
                # The recipe says "argument", this instruction says otherwise. Name the
                # disagreement rather than resolving it: the caller has to find the value,
                # and they now know why nothing here supplies it.
                entry["why"] = (
                    f"the recipe reads it from a `{head}` argument, but this instruction "
                    f"declares no `{head}` — it was carried from a sibling instruction, "
                    f"so the value must come from somewhere else. Do not invent it: a "
                    f"made-up seed derives a real, valid, wrong address."
                )
            elif not recipe.get("resolvable", True):
                entry["why"] = (
                    "the recipe for this account is flagged — nobody can derive it"
                )
            else:
                entry["why"] = (
                    "not an account or argument of this instruction; the caller supplies it"
                )
    return sorted(needs.values(), key=lambda n: n["value"])


def callability(instructions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Split instructions by what it would actually take to call them.

    ``needs`` non-empty used to mean four different things at once, and a 200-program
    sample of the live catalogue reported "17.7% blocked" because of it. Decomposed, that
    was roughly 8% with no derivable recipe at all and 10% where the recipe is known and a
    named value has to be fetched — plus a slice that was not blocked in any sense,
    because the caller constructs the value themselves.

    Those are three different problems with three different fixes: source recovery or
    on-chain enumeration, one chain read, and nothing. A single number that averages them
    is not a measurement.

    The buckets are mutually exclusive and total, and ``uncallable`` wins ties: when no
    recipe exists, a chain read cannot help because there is nothing to read into.
    """
    buildable = lookup = uncallable = 0
    for instruction in instructions:
        if instruction.get("unresolvable"):
            uncallable += 1
        elif instruction.get("needs"):
            lookup += 1
        else:
            buildable += 1
    return {
        "instructions": len(instructions),
        "buildable_now": buildable,
        "needs_a_lookup": lookup,
        "uncallable": uncallable,
    }


def build_artifact(
    idl: Mapping[str, Any],
    *,
    program_id: str | None = None,
    source: str | None = None,
    generated_from: str | None = None,
) -> dict[str, Any]:
    """One program's artifact, from its IDL (and optionally its source).

    ``generated_from`` names where the IDL came from, so a reader can tell a catalogue's
    copy from the program author's. Omitted rather than guessed when unknown — a
    provenance field that invents its own answer is worse than an absent one.
    """
    graph: ProgramGraph = build_program_graph(
        idl=dict(idl), source=source, program_id=program_id
    )
    rendered = graph.to_json()
    pdas = rendered["pdas"]

    encodings = {
        str(raw.get("name")): instruction_encoding(raw)
        for raw in idl.get("instructions", [])
        if isinstance(raw, Mapping)
    }

    instructions: list[dict[str, Any]] = []
    for instruction in rendered["instructions"]:
        pda_accounts = [a for a in instruction["accounts"] if a.get("is_pda")]
        instructions.append(
            {
                "name": instruction["name"],
                "args": instruction["args"],
                "encoding": encodings.get(instruction["name"], {}),
                "derivation_order": instruction["derivation_order"],
                "cycle": instruction["cycle"],
                "accounts": instruction["accounts"],
                "needs": instruction_needs(instruction, pdas),
                "pda_accounts": len(pda_accounts),
                "unresolvable": sorted(
                    a["name"] for a in pda_accounts if not a.get("resolvable", True)
                ),
            }
        )

    origins: dict[str, int] = {}
    for recipe in pdas.values():
        key = str(recipe.get("origin") or "unknown")
        origins[key] = origins.get(key, 0) + 1

    return {
        "gecko_artifact": ARTIFACT_VERSION,
        "program_id": rendered["program_id"],
        "generated_from": generated_from,
        "about": {
            "is": (
                "PDA seed recipes with their byte widths, per-account provenance, the "
                "order accounts must be derived in, and what a caller must fetch first."
            ),
            "is_not": (
                "not the program's own word beyond what its IDL states. A `recovered` "
                "recipe was carried from a sibling instruction that declares it "
                "derivably; it should be pinned by a chain read before anything "
                "irreversible relies on it."
            ),
            "carries_no": "response payloads, balances, user data or secrets",
        },
        "recipes": {
            name: {**recipe, "reads_as": [_seed_label(s) for s in recipe["seeds"]]}
            for name, recipe in pdas.items()
        },
        "instructions": instructions,
        "counts": {
            "instructions": len(instructions),
            "pda_recipes": len(pdas),
            "flagged_recipes": sum(
                1 for r in pdas.values() if not r.get("resolvable", True)
            ),
            "by_origin": origins,
            # Split, not summed: see `callability` for why one number here was wrong.
            "callability": callability(instructions),
        },
    }


# ---------------------------------------------------------------------------
# instruction encoding — the half comprehension was missing
# ---------------------------------------------------------------------------

#: Byte widths for the Anchor/Borsh scalars a caller can encode without a schema walk.
#: A type absent from here is not refused — it is reported with `fixed_size: null`, which
#: is the honest statement that its length depends on the value.
_SCALAR_WIDTHS: dict[str, int] = {
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
    "pubkey": 32,
    "publicKey": 32,
}


def _type_name(declared: Any) -> str:
    """A declared type rendered as Rust, across both IDL generations."""
    if isinstance(declared, str):
        return declared
    if isinstance(declared, Mapping):
        if "defined" in declared:
            defined = declared["defined"]
            if isinstance(defined, Mapping):
                return str(defined.get("name", "defined"))
            return str(defined)
        if "option" in declared:
            return f"Option<{_type_name(declared['option'])}>"
        if "vec" in declared:
            return f"Vec<{_type_name(declared['vec'])}>"
        if "array" in declared:
            inner = declared["array"]
            if isinstance(inner, list) and len(inner) == 2:
                return f"[{_type_name(inner[0])}; {inner[1]}]"
    return "unknown"


def _fixed_size(declared: Any) -> int | None:
    """The byte length of a value of this type, or ``None`` when it depends on the value.

    ``None`` is a real answer, not a failure: a `String` or a `Vec<T>` is length-prefixed
    and cannot be sized without the value. Reporting a guess would be worse than
    reporting nothing.
    """
    if isinstance(declared, str):
        return _SCALAR_WIDTHS.get(declared)
    if isinstance(declared, Mapping) and "array" in declared:
        inner = declared["array"]
        if isinstance(inner, list) and len(inner) == 2 and isinstance(inner[1], int):
            element = _fixed_size(inner[0])
            return element * inner[1] if element is not None else None
    return None


def instruction_encoding(instruction: Mapping[str, Any]) -> dict[str, Any]:
    """The discriminator and argument layout — how to turn a call into bytes.

    THIS IS THE HALF `comprehend_program` WAS MISSING, and a live agent named the
    consequence exactly: given recipes but no encoding, it declined to hand-roll the byte
    layout for an instruction that moves USDC, because "a wrong discriminator against a
    real program is exactly the failure your simulate-then-bind flow exists to prevent".
    Declining was right. Not having to decline is better.

    TWO INDEPENDENT SOURCES, AND THEY ARE CROSS-CHECKED. Anchor 0.30+ writes the
    discriminator into the IDL, and the same eight bytes are `sha256("global:<name>")[:8]`
    by convention. Agreement is reported as `verified`; the IDL alone as `declared`; the
    convention alone as `computed`. **Disagreement is never resolved by preference** — it
    is reported as `disagree` with both values, because a surface whose own two answers
    differ is exactly the case where picking one silently produces a call that fails
    against the real program.
    """
    import hashlib

    name = str(instruction.get("name", ""))
    declared = instruction.get("discriminator")
    declared = (
        [int(b) for b in declared]
        if isinstance(declared, (list, tuple)) and len(declared) == 8
        else None
    )
    computed = list(hashlib.sha256(f"global:{name}".encode()).digest()[:8])

    if declared is None:
        source, value = "computed", computed
    elif declared == computed:
        source, value = "verified", declared
    else:
        source, value = "disagree", declared

    args: list[dict[str, Any]] = []
    for arg in instruction.get("args", []) or []:
        if not isinstance(arg, Mapping):
            continue
        args.append(
            {
                "name": arg.get("name"),
                "type": _type_name(arg.get("type")),
                "fixed_size": _fixed_size(arg.get("type")),
            }
        )

    encoding: dict[str, Any] = {
        "discriminator": value,
        "discriminator_hex": bytes(value).hex(),
        "discriminator_source": source,
        "args": args,
        # Borsh in declaration order, little-endian, no padding — stated because an
        # agent that has the pieces still has to know the assembly rule.
        "layout": "discriminator(8) then each arg in order, Borsh little-endian",
    }
    if source == "disagree":
        encoding["declared"] = declared
        encoding["computed"] = computed
        encoding["warning"] = (
            f"the IDL declares a discriminator for {name!r} that is not "
            f'sha256("global:{name}")[:8]. Do not pick one — the surface disagrees with '
            "itself, and either choice may be the wrong call against the real program."
        )
    if any(a["fixed_size"] is None for a in args):
        encoding["note"] = (
            "one or more arguments are variable-length (String, Vec, Option): they are "
            "length-prefixed by Borsh, so the instruction data has no fixed size."
        )
    return encoding
