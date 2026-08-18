"""Projectors — Gecko's graph rendered into somebody else's execution format.

Comprehension produces ONE view model (:mod:`gecko.program_graph`'s
:class:`~gecko.program_graph.ProgramGraph`). A projector turns that view model into
a document a specific runtime can execute. It is a pure, total function of the
graph: no chain reads, no network, no hidden state — so the same graph always
projects to the same bytes, and a projection that cannot be made honestly refuses
instead of emitting something plausible.

Two projectors live here: :mod:`gecko.project.fdl` (Orquestra's Flow Definition
Language — a document another runtime executes) and :mod:`gecko.project.probes`
(the probe cases a scorecard runs against the program itself). They read the graph
through the same helpers (:mod:`gecko.project.graph_read`), because two projectors
that disagree about which arg a seed means derive two different accounts from one
graph. Everything a projector cannot express must raise a typed error naming the
fact it could not carry, never a default.
"""

from __future__ import annotations

from .fdl import (
    FdlProjectionError,
    InvalidSlugError,
    UnknownInstructionError,
    UnprojectableArgError,
    UnprojectableSeedError,
    UnresolvedSeedProjectionError,
    to_fdl,
)
from .probes import (
    MissingProbeBindingError,
    ProbeCase,
    ProbeError,
    ProbeValue,
    UnderivableAccountError,
    UnprobableArgTypeError,
    probe_case,
    probe_cases,
)

__all__ = [
    "FdlProjectionError",
    "InvalidSlugError",
    "MissingProbeBindingError",
    "ProbeCase",
    "ProbeError",
    "ProbeValue",
    "UnderivableAccountError",
    "UnknownInstructionError",
    "UnprobableArgTypeError",
    "UnprojectableArgError",
    "UnprojectableSeedError",
    "UnresolvedSeedProjectionError",
    "probe_case",
    "probe_cases",
    "to_fdl",
]
