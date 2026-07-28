"""verify-docs — check a surface's operations against reality, attach verdicts (VAS-2).

VAS-1 shipped the vocabulary (the claimed-until-checked provenance tier,
``graph.VerifyVerdict``, ``validator.verdict_from_replay``). This module is the driver that
walks every tool on a surface, replays it through the ALREADY-SHIPPED call path
(``client.call`` → ``caller`` / ``validator`` — carrying the auth-host pin, recorded-mode
scrub, and SSRF guard, so no new network path is introduced), lifts the outcome into a
``VerifyVerdict``, and attaches it to the operation's graph node (``Node.verify``) as a
SEPARATE axis from provenance (``graph.op_provenance`` is the single home of that axis).

Two modes, one code path (invariant #3):

* ``recorded`` (default): the response is synthesized from the schema, so no status ever
  comes off the wire. Every op is honestly **UNVERIFIED** — the offline, $0,
  falsifiable baseline. A recorded 200 is NEVER overclaimed as VERIFIED (the honesty
  flag lives in ``verdict_from_replay``: only ``source == "observed"`` can VERIFY/REFUTE).
* ``live``: the real upstream is called. A 2xx/3xx VERIFIES the op; a 404 (or any 4xx/5xx)
  REFUTES it — the fabricated-endpoint case (a docs source asserting an endpoint the API
  does not serve). If the surface degrades live→recorded (quarantine / no injectable auth),
  the actual mode is synthetic and the op stays UNVERIFIED (honest "no access").

Control plane only (invariant #1): the returned report carries op ids + verdict STATUS +
BASIS strings + a provenance category + counts. It never carries a response body, an arg
value, a filled URL, or a secret — none of those ever enter this module (the verdict is
lifted from a payload-free ``CallOutcome``, and the synthesized response ``data`` from a
recorded call is read for its STATUS only, never copied out).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import corpus
from .caller import CallError
from .graph import VerifyStatus, op_provenance
from .modes import CallMode
from .validator import example_args, verdict_from_replay

if TYPE_CHECKING:
    from .client import AgentApiClient

# op_provenance (the claimed-until-checked classifier) lives in graph.py — its home so the
# provenance ladder has a single source of truth; re-exported here as the verify surface.
__all__ = ["load_observed_corpus", "op_provenance", "verify_docs"]

_STATUS_TO_SUMMARY: dict[VerifyStatus, str] = {
    "VERIFIED": "verified",
    "REFUTED": "refuted",
    "UNVERIFIED": "unverified",
}

# Do-no-harm: a docs-verification probe must never MUTATE a live provider. Only
# idempotent-safe reads may reach the wire; a mutating method (POST/PUT/PATCH/DELETE) is
# forced to recorded even under ``--live`` — it stays honestly UNVERIFIED (no wire evidence)
# rather than firing a state-changing call the provider never consented to. This is a
# tightening, not a relaxation: it can only REMOVE a live call, never add one.
_LIVE_SAFE_METHODS = frozenset({"GET", "HEAD"})


def _replay_outcome(
    client: AgentApiClient, tool: dict[str, Any], mode: CallMode
) -> corpus.CallOutcome:
    """Replay one tool through the shipped call path and lift the result into a
    control-plane-safe ``CallOutcome`` (metadata only — status/shape, never a body).

    Reuses ``client.call`` so the auth-host pin, recorded-mode scrub, and SSRF guard all
    still apply (no new network path). The ACTUAL mode returned by ``call`` — not the
    requested one — feeds the outcome's source: a live call that degraded to recorded
    (quarantine / no injectable auth) is honestly synthetic, so it can never VERIFY.
    """
    tool_name = tool["name"]
    op = client._op_by_name[tool_name]
    args = example_args(tool)
    invoke = tool.get("_invoke")
    if not isinstance(invoke, dict):
        invoke = {"method": op.method, "path": op.path}

    # Do-no-harm on the verify path: never let a mutating method reach a live provider.
    # Downgrade live -> recorded for anything but a safe read, so the probe observes the
    # method without ever changing provider state (see ``_LIVE_SAFE_METHODS``).
    if mode == "live" and op.method.upper() not in _LIVE_SAFE_METHODS:
        mode = "recorded"

    status: int | None = None
    exc: CallError | None = None
    actual_mode: str = mode
    try:
        result = client.call(tool_name, args, mode=mode)
        status = result.get("status")
        # honest source: a live call that degraded shows its real (recorded) mode here.
        actual_mode = result.get("mode", mode)
    except CallError as err:  # pre-flight failure (missing input / auth-host refusal)
        exc = err

    return corpus.outcome_from(
        operation_id=op.operation_id,
        tool_invoke=invoke,
        args=args,
        status=status,
        error_class=corpus.error_class_for(status, exc),
        latency_ms=None,
        mode=actual_mode,
        auth_injected=False,
        ts=int(time.time() * 1000),
        surface_id=client.surface_id,
        surface_rev=client.surface_rev,
    )


def load_observed_corpus(path: str | Path) -> dict[str, corpus.CallOutcome]:
    """Load a pre-captured observed-outcome corpus (JSONL) into an ``op_id -> CallOutcome``
    map — the offline replay seam for the verify demo (Pattern B).

    These are REAL statuses that came off the wire in a prior ``--live`` capture, replayed
    here so the demo can falsify offline before any live smoke — they are NEVER synthesized
    (the honesty gate in ``verdict_from_replay`` still only trusts ``source == "observed"``,
    which this fixture legitimately carries because a live capture derived it). Each row is
    rehydrated through ``corpus.outcome_from_record``, which fails closed on any
    non-allowlisted key, so a payload or secret cannot ride in through the fixture."""
    outcomes: dict[str, corpus.CallOutcome] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        outcome = corpus.outcome_from_record(json.loads(line))
        outcomes[outcome.operation_id] = outcome
    return outcomes


def verify_docs(
    client: AgentApiClient,
    *,
    mode: CallMode = "recorded",
    outcomes: Mapping[str, corpus.CallOutcome] | None = None,
) -> dict[str, Any]:
    """Verify every operation on the surface against reality and attach the verdicts.

    For each usable tool: obtain an outcome, lift it with VAS-1's ``verdict_from_replay``,
    attach the ``VerifyVerdict`` to the op's graph node (``Node.verify`` — a separate axis;
    provenance is left untouched), and record a control-plane-safe entry ``{status, basis,
    provenance}`` in the report.

    The outcome comes from one of two sources, and NEITHER weakens the honesty gate:

    * ``outcomes`` (opt-in): a pre-captured ``op_id -> CallOutcome`` map — a REAL prior run
      replayed offline (``gecko.verify.load_observed_corpus``). An op present in the map uses
      its observed outcome; any op absent falls back to a fresh replay. This is the
      offline-falsifiable path (Pattern B): the demo REFUTES a fabricated endpoint with no
      network, because the observed 404 already happened.
    * fresh replay (default): ``_replay_outcome`` runs the op through the shipped call path
      in ``mode``. Recorded (default) → every op UNVERIFIED (no wire evidence); live → real
      VERIFIED / REFUTED.

    Returns ``{"mode", "report": {op_id: {...}}, "summary": {verified, refuted,
    unverified}}``. Never returns a payload, value, or secret.
    """
    graph = client.surface_graph
    report: dict[str, dict[str, Any]] = {}
    summary = {"verified": 0, "refuted": 0, "unverified": 0}

    for tool in client.list_tools():
        op = client._op_by_name.get(tool["name"])
        if op is None:  # a synthetic tool with no backing operation — nothing to verify
            continue
        op_id = op.operation_id
        captured = outcomes.get(op_id) if outcomes is not None else None
        outcome = (
            captured if captured is not None else _replay_outcome(client, tool, mode)
        )
        verdict = verdict_from_replay(outcome)

        # Attach the verdict to the op node. Node is frozen because its identity (the
        # content hash) is derived from the SHAPE; ``verify`` is deliberately excluded
        # from that hash (a drift-dependent replay result), so setting it post-hoc via
        # object.__setattr__ is the intended out-of-hash mutation, not a contract break.
        node = graph._by_id.get(graph.opnode(op_id))
        if node is not None:
            object.__setattr__(node, "verify", verdict)

        report[op_id] = {
            "status": verdict.status,
            "basis": list(verdict.basis),
            # provenance is a SEPARATE axis — never overwritten by the verdict.
            "provenance": op_provenance(client.spec, op_id),
        }
        summary[_STATUS_TO_SUMMARY[verdict.status]] += 1

    return {"mode": mode, "report": report, "summary": summary}
