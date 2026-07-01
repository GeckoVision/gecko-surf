"""gecko.redteam — the off-chain battle-test benchmark for agent decisions.

Layers, bottom-up: the 12 scenarios as immutable DATA (``scenarios``), the harm-oracle +
2x2 scorecard (``scorer``), the decision seam + two defense arms + run loop (``harness``),
and the paired $0 ScriptedPolicies that give CI a deterministic pass/fail (``policies``).
"""

from __future__ import annotations

from .harness import (
    Arm,
    BuiltSurface,
    Ctx,
    Decision,
    DefendedArm,
    HarnessError,
    NaiveArm,
    Policy,
    ScriptedPolicy,
    canary_session,
    quarantine_observations,
    run_scenario,
    run_suite,
)
from .policies import defended_policy, naive_policy
from .report import (
    as_dict,
    render,
    render_headline,
    render_json,
    write_audit,
)
from .scenarios import (
    LAYERS,
    PREDICATES,
    SCENARIOS,
    VECTORS,
    Scenario,
    apply_spec_patch,
)
from .scorer import (
    Harm,
    Scorecard,
    evaluate_predicate,
    judge,
    score,
    verdict_for,
)

__all__ = [
    "LAYERS",
    "PREDICATES",
    "SCENARIOS",
    "VECTORS",
    "Arm",
    "BuiltSurface",
    "Ctx",
    "Decision",
    "DefendedArm",
    "Harm",
    "HarnessError",
    "NaiveArm",
    "Policy",
    "Scenario",
    "Scorecard",
    "ScriptedPolicy",
    "apply_spec_patch",
    "as_dict",
    "canary_session",
    "defended_policy",
    "evaluate_predicate",
    "judge",
    "naive_policy",
    "quarantine_observations",
    "render",
    "render_headline",
    "render_json",
    "run_scenario",
    "run_suite",
    "score",
    "verdict_for",
    "write_audit",
]
