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

from typing import Any, Mapping

from .program_graph import ProgramGraph, build_program_graph

__all__ = [
    "ARTIFACT_VERSION",
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
            if "." in seed and source == "account":
                entry["why"] = (
                    f"a field of the `{head}` account — read it on-chain and pass it in"
                )
            elif "." in seed and source == "argument":
                entry["why"] = (
                    f"a field of the `{head}` argument struct — you build it, so pass "
                    f"`{seed}` directly"
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

    instructions: list[dict[str, Any]] = []
    for instruction in rendered["instructions"]:
        pda_accounts = [a for a in instruction["accounts"] if a.get("is_pda")]
        instructions.append(
            {
                "name": instruction["name"],
                "args": instruction["args"],
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
            "instructions_needing_a_chain_read": sum(
                1 for i in instructions if i["needs"]
            ),
        },
    }
