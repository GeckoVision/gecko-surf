"""Inline enforcement gate — promote the semantic risk SCORE to an ENFORCED
allow / step-up / block decision at call time.

``risk.py`` computes *what* a call's risk is (a composite 0-100 score + a decision +
human reasons); this module owns *what to do about it* at the hosted call boundary:
resolve the operator's enforcement stance, turn a decided ``block`` into a structured
refusal the agent can read, and turn a ``step_up`` (or a soft warn-mode block) into a
non-fatal warning attached to the result.

WHAT THIS ENFORCES (and what it does NOT): it enforces the SCORED, comprehension-native
signals at call time — schema-conformance (malformed for THIS API), injection markers in
metadata/args, an exfil host, provenance, and intent/scope — complementing the ingest/
prepare wall (``sanitize`` + ``caller`` + ``netguard``). It does NOT inspect response
PAYLOADS (invariant #1 — that class stays a deliberate GAP), and it is not a general
prompt-injection solver. It makes the "we block poisoned / malformed / exfil CALLS" claim
true for the hosted surface.

The env toggle ``GECKO_ENFORCE`` picks the stance:

* ``block`` — a decided ``block`` is REFUSED (the upstream API is never called) and a
  ``surf.blocked`` event is emitted; a ``step_up`` executes with a warning attached.
* ``warn``  — nothing is hard-blocked; a would-be block or a ``step_up`` executes with a
  warning attached (observe-only — the measure phase before enforce).
* ``off``   — the gate is bypassed entirely (no scoring runs).

FAIL-SAFE STANCE: if the SCORER ITSELF raises, the caller logs and ALLOWS (fail-open — a
scoring bug must never break the product). But a *decided* block always blocks: fail-open
only covers the "we couldn't score it" case, never a "we scored it as dangerous" case.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from .risk import RiskAssessment

#: The operator's enforcement stance. Single source of truth; consumers import it.
EnforceMode = Literal["block", "warn", "off"]

_MODES: frozenset[str] = frozenset({"block", "warn", "off"})

#: The hosted surface enforces by default; a plain/local/CLI surface only warns (least
#: surprise — a bare install must not silently start refusing calls).
HOSTED_DEFAULT: EnforceMode = "block"
LOCAL_DEFAULT: EnforceMode = "warn"


def enforce_mode_from_env(default: EnforceMode = LOCAL_DEFAULT) -> EnforceMode:
    """Resolve ``GECKO_ENFORCE`` to a mode, falling back to ``default`` when unset or
    invalid (fail-safe: an unrecognized value never silently disables the gate)."""
    raw = os.environ.get("GECKO_ENFORCE", "").strip().lower()
    if raw in _MODES:
        return raw  # type: ignore[return-value]
    return default


def resolve_hosted_enforce(explicit: EnforceMode | None = None) -> EnforceMode:
    """The SINGLE place the hosted serving default is resolved. An explicit stance wins;
    otherwise ``GECKO_ENFORCE``, falling back to the hosted ``block`` default. Both the
    single-surface and multi-surface hosted servers call this, so a hosted deploy can
    never silently default to different stances (the reviewer's serve_http→warn vs
    multi-surface→block divergence)."""
    if explicit is not None:
        return explicit
    return enforce_mode_from_env(HOSTED_DEFAULT)


@dataclass(frozen=True)
class GateOutcome:
    """What the gate decided to DO, transport-agnostic — so ``mcp_server.call_tool`` (and
    any future call boundary) shares ONE dispatch. ``blocked`` means the upstream call is
    refused; ``warn`` means it executes but a warning is attached. ``assessment`` is the
    scored risk (``None`` when the gate was off or scoring failed open)."""

    blocked: bool
    warn: bool
    assessment: RiskAssessment | None


def apply_gate(assessment: RiskAssessment | None, mode: EnforceMode) -> GateOutcome:
    """Promote a scored assessment to an enforcement action — the ONE dispatch point.

    ``off`` or a failed-open (``None``) assessment is a pass-through. A decided ``block``
    hard-blocks only in ``block`` mode; a ``step_up`` — or a ``warn``-mode would-be block —
    executes with a warning attached. This never re-scores; ``risk.py`` already decided the
    categorical/threshold verdict, this only maps verdict × mode → action."""
    if mode == "off" or assessment is None:
        return GateOutcome(blocked=False, warn=False, assessment=assessment)
    if assessment.decision == "block" and mode == "block":
        return GateOutcome(blocked=True, warn=False, assessment=assessment)
    return GateOutcome(
        blocked=False, warn=assessment.decision != "allow", assessment=assessment
    )


def refusal_payload(assessment: RiskAssessment) -> dict[str, Any]:
    """The structured refusal returned to the AGENT for a hard block — so it learns WHY
    (human reasons) rather than getting an opaque error. This flows back in the JSON-RPC
    reply and is never persisted (like a response body); the telemetry record instead
    carries only the code-constant signal NAMES (see ``blocked_signals``)."""
    return {
        "blocked": True,
        "decision": "block",
        "score": assessment.score,
        "reasons": [r.message for r in assessment.reasons],
    }


def warning_payload(assessment: RiskAssessment) -> dict[str, Any]:
    """The non-fatal warning attached to a result for a ``step_up`` (or a warn-mode
    would-be block): the call executed, but the agent/operator is told it was elevated."""
    return {
        "decision": assessment.decision,
        "score": assessment.score,
        "reasons": [r.message for r in assessment.reasons],
    }


def attach_warning(result: Any, assessment: RiskAssessment) -> Any:
    """Attach the warning to a result dict under ``gecko_risk`` (non-destructive to the
    upstream payload). A non-dict result is returned unchanged (nothing to attach to)."""
    if isinstance(result, dict):
        result["gecko_risk"] = warning_payload(assessment)
    return result


def blocked_signals(assessment: RiskAssessment) -> list[str]:
    """The control-plane-safe reason set for telemetry: the SIGNAL names only (code
    constants like "poison.injection"), never the human ``.message`` — which can embed an
    arg-derived value (a host, an enum value). Capped defensively to the event allowlist."""
    from .events import MAX_REASONS

    seen: list[str] = []
    for r in assessment.reasons:
        if r.signal not in seen:
            seen.append(r.signal)
    return seen[:MAX_REASONS]


__all__ = [
    "EnforceMode",
    "GateOutcome",
    "HOSTED_DEFAULT",
    "LOCAL_DEFAULT",
    "apply_gate",
    "attach_warning",
    "blocked_signals",
    "enforce_mode_from_env",
    "refusal_payload",
    "resolve_hosted_enforce",
    "warning_payload",
]
