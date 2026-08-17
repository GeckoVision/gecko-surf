"""Every way a projection can refuse, as one typed hierarchy.

They live in their own module because both halves of the projector raise them —
:mod:`gecko.project.seeds` (the seed vocabulary) and :mod:`gecko.project.fdl` (the
document) — and a shared type declared twice is a type that drifts. The public
surface re-exports them from :mod:`gecko.project.fdl`, so a caller only ever
imports one name.

Messages carry only public data — account names, seed names, program addresses. A
PDA recipe has no secret material by construction (invariant #1).
"""

from __future__ import annotations

__all__ = [
    "FdlProjectionError",
    "InvalidSlugError",
    "UnknownInstructionError",
    "UnprojectableArgError",
    "UnprojectableSeedError",
    "UnresolvedSeedProjectionError",
]


class FdlProjectionError(Exception):
    """Base class for every refusal to project a graph into FDL."""


class UnknownInstructionError(FdlProjectionError):
    """The graph has no instruction by that name."""


class UnresolvedSeedProjectionError(FdlProjectionError):
    """A PDA seed does not resolve to anything this instruction supplies.

    THE refusal. FDL would happily take ``{kind: "string", value: "$inputs.foo"}``
    for a seed whose real value is a runtime read or a name that does not exist —
    it compiles, it derives, and the address is wrong. Nothing downstream can tell
    that apart from a correct one: the flow lints, the transaction builds, and the
    failure surfaces as a constraint error against an account nobody meant. So it
    stops here, naming the account and the seed.
    """


class UnprojectableSeedError(FdlProjectionError):
    """The seed is resolvable but FDL has no encoding that reproduces its bytes —
    a min/max ordered pair, a big-endian integer, an integer whose signedness the
    graph cannot recover, raw bytes with no input type to carry them."""


class UnprojectableArgError(FdlProjectionError):
    """An instruction arg (or a caller-supplied override) has no FDL input type."""


class InvalidSlugError(FdlProjectionError):
    """``meta.slug`` is not kebab-case, which his schema rejects."""
