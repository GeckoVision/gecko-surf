"""Call-outcome capture — the usage-telemetry + opt-in correctness-corpus edge.

Split out of ``client.py`` so the capture concern (what we learn from a call) stays
separate from the call path itself (how the call is made) — the file the graph work
keeps extending. One entry point, ``capture_outcome``, invoked by the client after
EVERY call outcome (success, failure, pre-flight raise) in every mode.

Control-plane only (invariant #1): one metadata record per call — status, error
CLASS, latency, mode/source provenance — never a response body, never a filled URL,
never an arg value beyond what ``corpus.outcome_from`` structurally allowlists. A
capture failure must never break the agent's call (``corpus.record`` is best-effort
by contract).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import corpus
from .events import emit_surf_event


def capture_outcome(
    *,
    tool_name: str,
    status: int | None,
    exc: BaseException | None,
    args: dict[str, Any],
    latency_ms: int | None,
    mode: str,
    surface_id: str,
    surface_rev: str,
    corpus_path: str | Path | None,
    tool: dict[str, Any] | None,
    auth_injected: bool,
) -> None:
    """Emit the usage event and (opt-in) append one control-plane-safe corpus record.

    Uses the SAME narrow ``corpus.outcome_from`` boundary the HTTP server uses (it
    structurally cannot receive a payload). Corpus capture is opt-in via
    ``corpus_path``; the usage event always fires (and is itself a no-op without a
    telemetry sink)."""
    # Usage instrumentation (independent of opt-in corpus capture): one
    # control-plane-safe outcome event — the ok-bool + error CLASS, never a body.
    # ``source`` carries the SAME provenance the corpus record derives (recorded ->
    # synthetic, live -> observed), so the adoption FCC rate can filter observed-only
    # and a faked recorded 200 never inflates it.
    error_class = corpus.error_class_for(status, exc)
    source = corpus.source_for_mode(mode)
    # plane="engine": this fires on EVERY client call outcome — local $0 flows
    # (demo, `gecko test`, recorded) included — whereas surf.call is a SURFACE
    # event; see events.CallPlane for why all-time fcc > call is expected.
    emit_surf_event(
        "surf.first_call_correct",
        surface_id=surface_id,
        tool_name=tool_name,
        mode=mode,
        ok=status is not None and 200 <= status < 400,
        error_class=error_class,
        latency_ms=latency_ms,
        source=source,
        plane="engine",
    )
    if corpus_path is None:
        return
    invoke = tool.get("_invoke") if isinstance(tool, dict) else None
    if not isinstance(invoke, dict):
        return
    corpus.record(
        corpus.outcome_from(
            operation_id=tool_name,
            tool_invoke=invoke,
            args=args,
            status=status,
            error_class=error_class,
            latency_ms=latency_ms,
            mode=mode,
            auth_injected=auth_injected,
            ts=int(time.time() * 1000),
            surface_id=surface_id,
            surface_rev=surface_rev,
        ),
        corpus_path,
    )
