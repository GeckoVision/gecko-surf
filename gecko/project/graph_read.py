"""The questions every projector asks a :class:`~gecko.program_graph.ProgramGraph`.

Two of them must have exactly ONE answer in this package, because two projectors
that answer them differently produce two different transactions from one graph:

- *which instruction does this name refer to* — trivial, but the refusal (and the
  list of what does exist) is part of the contract.
- *which arg does this seed mean* — the ``mark_as_delivered`` case, where the seed
  says ``store_name`` and the instruction declares ``_store_name``. If the FDL
  projector resolved that alias and the probe projector did not (or resolved it to
  something else), the flow and the probe that is supposed to check the flow would
  derive two different store accounts, and the disagreement would read as a bug in
  the program rather than in this package.

So both live here and neither projector keeps a copy.
"""

from __future__ import annotations

from ..program_graph import InstructionGraph, ProgramGraph
from .errors import UnknownInstructionError

__all__ = ["arg_aliases", "find_instruction"]


def find_instruction(graph: ProgramGraph, name: str) -> InstructionGraph:
    """The instruction called ``name``, or a refusal that names what does exist."""
    for ix in graph.instructions:
        if ix.name == name:
            return ix
    known = ", ".join(sorted(i.name for i in graph.instructions)) or "(none)"
    raise UnknownInstructionError(
        f"instruction {name!r} is not in this program graph; known: {known}"
    )


def arg_aliases(ix: InstructionGraph) -> dict[str, str]:
    """Reconcile a seed that names an arg the instruction spells with a leading
    underscore — Rust's "declared but unused" convention, which Anchor copies
    verbatim into the IDL while the ``pda.seeds`` block keeps the ORIGINAL name.

    ``mark_as_delivered`` is the live case: its receipts seed points at
    ``store_name`` and its args are ``_store_name`` / ``receipt_id``, so a deriver
    that matches seeds to args by name finds nothing for the one seed that selects
    the store. This is a rename, not a guess — the alias must be the seed name with
    exactly one underscore added or removed, and must be the ONLY such candidate.
    Anything less certain falls through to the caller's refusal, which is the point:
    an unresolved seed becomes a wrong address that derives, compiles and simulates.

    The FDL projector records what it applied in ``x-gecko.carriedOutsideFdl.
    argAliases`` so a reader can audit the rename against the IDL.
    """
    declared = {name for name, _ in ix.args}
    aliases: dict[str, str] = {}
    for account in ix.accounts:
        for binding in account.derive_from:
            if binding.kind != "unresolved" or binding.seed_name in declared:
                continue
            seed = binding.seed_name
            candidates = [c for c in (f"_{seed}", seed.lstrip("_")) if c in declared]
            if len(set(candidates)) == 1:
                aliases[seed] = candidates[0]
    return aliases
