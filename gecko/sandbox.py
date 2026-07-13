"""Probe sandbox — offline validation that answers with the API's OWN error shape.

The self-healing loop's engine-safe core: ``evaluate`` runs an agent's call
through validation gates derived entirely from the comprehended spec and returns
a *synthetic result* either way — a malformed call yields the API's own declared
error body (422) plus machine-readable signals + remediation, a well-formed call
yields a schema-synthesized success. Nothing here ever reaches the wire, injects
auth, or persists anything: this module sits on the no-wire side of the transport
edge (invariant #3) and, by structural gate (see ``test_sandbox_evaluate``), has
no outcome-record call site at all — capture stays in the client, where probe
outcomes route ``source="synthetic"`` and never touch a published metric.

Gates (in order):
  (a) structural — declared-required presence (``caller._missing_required`` as a
      RESULT, not an exception);
  (b) schema — the comprehension-native conformance check (``risk._schema_conformance``:
      type/enum/unknown-field against the API's own schema);
  (c) state — the per-session ``SimWorld`` (deposit→withdraw correlations); lands
      with the SimWorld phase, the ``world`` seam is already in the signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .caller import _missing_required
from .client import _error_schema, _success_schema
from .enforce import REMEDIATION
from .ingest import Operation
from .risk import _schema_conformance
from .sample import example_from_schema
from .sanitize import sanitize_schema
from .tools import to_tool

#: Marks every probe result unmistakably synthetic to the agent (the recorded-mode
#: lesson: an agent cannot tell a zeroed placeholder from real data unless told).
PROBE_MODE_NOTE = (
    "Synthetic probe result — validated offline against the API's own schema; "
    "no live call was made and no real data is returned. Fix the reported "
    "signals and retry, or switch to live mode for real responses."
)

#: Fallback error body when the spec declares no 4xx/default error schema. A CODE
#: CONSTANT — it never interpolates an arg value, so it is control-plane safe.
_GENERIC_ERROR_BODY: dict[str, str] = {"error": "invalid request (synthetic probe)"}

#: The synthetic status for a call that fails validation. Fixed at 422 (the
#: canonical "well-formed but semantically invalid" code); ``_error_schema`` scans
#: 422 first so the body shape aligns whenever the API declares one.
_VALIDATION_STATUS = 422


@dataclass(frozen=True)
class SimResult:
    """One synthetic probe outcome — always a result, never an exception.

    ``data`` is synthesized from the spec's own response schemas (sanitized), so it
    can never carry a real payload; ``signals``/``remediation`` are code-constant
    names and generic fix strings (no arg values) — the agent's self-heal input."""

    status: int
    data: Any
    signals: list[str] = field(default_factory=list)
    remediation: dict[str, str] = field(default_factory=dict)
    mode: Literal["probe"] = "probe"
    mode_note: str = PROBE_MODE_NOTE


def _synthesize(schema: dict[str, Any]) -> Any:
    """Schema -> sanitized example. Response-side scrub (``route_to_arg=False``),
    the same defense recorded mode applies: a poisoned response schema must not
    surface an injected instruction or secret through the synthetic body."""
    clean, _ = sanitize_schema(schema, route_to_arg=False)
    return example_from_schema(clean)


def evaluate(
    op: Operation, args: dict[str, Any], world: Any | None = None
) -> SimResult:
    """Run one probe call through the gates and synthesize the outcome.

    ``world`` is the per-session SimWorld seam (the state gate); accepted now so
    the signature is stable, unused until that gate lands.
    """
    del world  # the state gate (c) consumes this when SimWorld lands
    tool = to_tool(op)
    schema = tool.get("inputSchema") or {}

    signals: list[str] = []
    # gate (a): declared-required presence — the same check the caller enforces
    # pre-flight, surfaced here as a result instead of a raised CallError.
    if _missing_required(tool, args):
        signals.append("schema.required")
    # gate (b): conformance against the API's own schema (type / enum / unknown).
    for reason in _schema_conformance(schema, args):
        if reason.signal not in signals:
            signals.append(reason.signal)

    if signals:
        data = _synthesize(_error_schema(op))
        if data is None:
            data = dict(_GENERIC_ERROR_BODY)
        return SimResult(
            status=_VALIDATION_STATUS,
            data=data,
            signals=signals,
            remediation={s: REMEDIATION[s] for s in signals if s in REMEDIATION},
        )

    # gate (c): state (SimWorld) — lands with the correlations phase.
    return SimResult(status=200, data=_synthesize(_success_schema(op)))
