"""Honeypot tripwire — decoy tools a *comprehended* surface never emits.

A honeypot plants a small, fixed set of DECOY tools with names no real comprehension
would ever produce (``admin_export``, ``dump_credentials`` …). A benign agent — one
that only routes task-shaped intent at the real, question-shaped tools — has no reason
to call one; a *probing* agent that enumerates the surface and calls a decoy trips the
wire. Any *call* of a decoy is, by construction, a near-zero-false-positive adversary
signal.

HONEST SCOPE (do not oversell this):

* A honeypot is a DETECTION TRIPWIRE, NOT A MOAT. It flags an agent that *probes*
  (enumerates + calls a decoy). It does NOT stop a targeted first-shot attack — an
  attacker who already knows the one real call makes it once and never touches a decoy.
* It is COPYABLE. Anyone can plant a fake ``admin_export``. The only compounding edge
  is the adversary corpus built from hits, and that flywheel is unproven.
* Any reputation it produces stays ADVISORY — never an auto-blocklist (a benign agent
  that fat-fingers a decoy must not be locked out).

OFF BY DEFAULT. A real hosted surface must not show fake tools unless the operator
opts in (``McpSurface(honeypots=True)`` or ``GECKO_HONEYPOTS=1``). When off, the
surface is byte-identical to one with no honeypot layer at all.

CONTROL-PLANE: a decoy touch records ONLY the code-constant signal + the sanitized
correlation fingerprint (``session_id``) + the decoy NAME (spec-derived, a code
constant). It records NO args and NO decoy payload — there is no upstream to call, so
there is no payload; the decoy's "output" is never synthesized or stored. Invariant #1
holds by construction.
"""

from __future__ import annotations

import os
from typing import Any

#: The control-plane-safe reason recorded on a decoy touch. A code constant (never an
#: arg value), so it passes the ``events`` label validator like any risk-signal name.
HONEYPOT_REASON = "honeypot.decoy_called"

#: The ``surf.blocked`` decision LABEL for a decoy touch. Short, non-secret — passes the
#: shape-only ``decision`` label validation in ``events`` (no closed-set change needed).
HONEYPOT_DECISION = "honeypot"

# The env truthy set — off unless EXPLICITLY enabled (parallels the enforce default).
_TRUE = frozenset({"1", "true", "yes", "on"})

# The fixed decoy set. Each NAME is one a real comprehension NEVER synthesizes (there is
# no originating operation), so no benign task can route intent toward it — the
# low-false-positive design. Descriptions are plausible-but-tempting so an *enumerating*
# adversary sees a target; a benign agent listing + reading them is NOT flagged (only a
# CALL trips the wire).
_DECOYS: tuple[tuple[str, str], ...] = (
    (
        "admin_export",
        "Export the full administrative dataset — all users, records, and internal state.",
    ),
    (
        "dump_credentials",
        "Dump stored API credentials and access tokens for every connected service.",
    ),
    (
        "export_all_secrets",
        "Export every secret, signing key, and environment variable on this surface.",
    ),
    (
        "disable_safety",
        "Disable all safety, validation, and rate-limit checks for subsequent calls.",
    ),
)

#: Fast membership form of the decoy names.
_DECOY_NAMES: frozenset[str] = frozenset(name for name, _ in _DECOYS)

# A minimal, well-formed input schema — the decoy must LOOK like a real tool so an
# enumerating agent finds it credible. The single boolean is inert: nothing reads it.
_DECOY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "description": "Confirm the operation.",
        }
    },
}


def is_decoy(name: str) -> bool:
    """True iff ``name`` is one of the planted decoys. Pure membership check."""
    return name in _DECOY_NAMES


def decoy_tool_defs() -> list[dict[str, Any]]:
    """The decoy tool defs, in the same shape as ``McpSurface.list_tools`` entries
    (``name`` / ``description`` / ``inputSchema``). A fresh copy per call — the caller
    may not mutate the module's fixed set."""
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": dict(_DECOY_SCHEMA),
        }
        for name, description in _DECOYS
    ]


def honeypots_from_env() -> bool:
    """Resolve ``GECKO_HONEYPOTS`` — OFF unless explicitly enabled (parallels the
    enforce env default). Any non-truthy/unset value means off."""
    return os.environ.get("GECKO_HONEYPOTS", "").strip().lower() in _TRUE


def honeypot_refusal() -> dict[str, Any]:
    """The generic block payload returned to the AGENT when it calls a decoy — same
    shape as ``enforce.refusal_payload`` (blocked/decision/score/reasons). Carries a
    human reason (never persisted, like a response body); the telemetry record instead
    carries only the code-constant signal name."""
    return {
        "blocked": True,
        "decision": HONEYPOT_DECISION,
        "score": 100,
        "reasons": ["This tool is a decoy; calling it is treated as hostile probing."],
    }


__all__ = [
    "HONEYPOT_DECISION",
    "HONEYPOT_REASON",
    "decoy_tool_defs",
    "honeypot_refusal",
    "honeypots_from_env",
    "is_decoy",
]
