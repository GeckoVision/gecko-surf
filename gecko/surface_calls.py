"""The ``surf.call`` record for surfaces that do not emit their own.

``McpSurface.call_tool`` emits ``surf.call`` itself. The duck-typed surfaces (the
course, the orquestra catalog, the meta front door, program graphs) do not, so until
2026-09-28 a call to ``/course/mcp`` left an access-log line and nothing else: no tool,
no outcome, no session. The transport calls :func:`record_surface_call` for exactly
those surfaces, so each served call is counted once whichever class answered it.

Only metadata leaves: the tool NAME, an ok flag, a closed error class, latency and the
opaque session id. The arguments and the result are read for one bit (did it fail?)
and never forwarded, which is invariant #1 enforced by ``events.ALLOWED_FIELDS``.
"""

from __future__ import annotations

import logging
from typing import Any

from . import corpus
from .events import emit_surf_event
from .telemetry import TelemetryError
from .toolerror import is_upstream_failure

logger = logging.getLogger("gecko.surface_calls")


def call_outcome(result: Any, exc: BaseException | None) -> tuple[bool, str]:
    """``(ok, error_class)`` for one served call, from its shape only.

    An HTTP status keeps its precise class. A structured refusal without a status (a
    program surface's ``{"error": ...}``) is ``other``: the error text is a payload and
    ``error_class`` is a closed set, so there is nothing more precise to say safely.
    """
    if exc is not None:
        return False, corpus.error_class_for(None, exc)
    if not is_upstream_failure(result):
        return True, "none"
    status = result.get("status") if isinstance(result, dict) else None
    if isinstance(status, int) and not isinstance(status, bool):
        return False, corpus.error_class_for(status, None)
    return False, "other"


def record_surface_call(
    *,
    surface_id: str,
    tool_name: str,
    result: Any,
    exc: BaseException | None,
    latency_ms: int,
    session_id: str | None,
) -> None:
    """Emit one ``surf.call`` for a duck-typed surface. Never raises into the call.

    A control-plane violation normally must surface (``emit_surf_event`` re-raises it).
    Here the call has ALREADY happened, possibly with a side effect, so raising would
    throw away a real result to protect a metrics row; the row is dropped and a
    redacted warning left instead. The tool name is already known-good at this point
    (``install_unknown_tool_gate`` refuses unknown names before the handler runs).
    """
    ok, error_class = call_outcome(result, exc)
    try:
        emit_surf_event(
            "surf.call",
            surface_id=surface_id,
            tool_name=tool_name,
            ok=ok,
            error_class=error_class,
            latency_ms=latency_ms,
            session_id=session_id,
            plane="surface",
        )
    except TelemetryError:
        logger.warning("surf.call not recorded: field failed the allowlist (redacted)")


__all__ = ["call_outcome", "record_surface_call"]
