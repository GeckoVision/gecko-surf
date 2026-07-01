"""gecko.redteam — the off-chain battle-test benchmark for agent decisions.

v1 foundations (this step): the 12 scenarios as immutable DATA + closed axes. The harness,
scorer, report, and CLI are added on top of these without changing the data contract.
"""

from __future__ import annotations

from .scenarios import (
    LAYERS,
    PREDICATES,
    SCENARIOS,
    VECTORS,
    Scenario,
    apply_spec_patch,
)

__all__ = [
    "LAYERS",
    "PREDICATES",
    "SCENARIOS",
    "VECTORS",
    "Scenario",
    "apply_spec_patch",
]
