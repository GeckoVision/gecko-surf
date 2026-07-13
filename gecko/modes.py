"""Canonical call-mode type — the single source of truth.

Project rule: a shared ``Literal`` lives in ONE module and every consumer imports
it; it is never redeclared. Three modes, one code path (invariant #3) — they
diverge only at the transport edge:

- ``recorded``: synthesize the response from the spec ($0, offline, falsifiable).
- ``live``: really call the upstream API (auth injected at the transport edge).
- ``probe``: the offline sandbox — recorded plus a validation pre-gate that answers
  a malformed call with the API's OWN synthetic error (see ``gecko.sandbox``).
  No wire, no auth injection; outcomes route ``source="synthetic"`` and are
  structurally excluded from any published metric.
"""

from __future__ import annotations

from typing import Literal, cast, get_args

CallMode = Literal["recorded", "live", "probe"]

CALL_MODES: frozenset[str] = frozenset(get_args(CallMode))


def coerce_mode(value: str | None, default: CallMode = "recorded") -> CallMode:
    """Validate an untyped mode string (the env/CLI boundary) into a ``CallMode``.

    Fails CLOSED to the $0 offline default: the engine already treats any unknown
    mode as recorded synthesis (``call`` only fires the wire on exactly ``live``),
    so a typo can degrade to offline but can never escalate to a live call.
    """
    if value in CALL_MODES:
        return cast(CallMode, value)
    return default
