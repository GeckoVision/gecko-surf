"""Correctness validator + outcome log (the flywheel seed).

For every generated tool, synthesize valid inputs from its schema and confirm the
comprehension layer produces a *well-formed* request (catching the silent
first-call failure the agent can't see). Each outcome is appended to a JSONL log
— in live mode this accumulates the "how API X is actually called correctly"
corpus that compounds into the moat.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import corpus
from .caller import CallError, build_request
from .client import AgentApiClient
from .graph import VerifyVerdict
from .sample import example_from_schema


def verdict_from_replay(outcome: corpus.CallOutcome) -> VerifyVerdict:
    """Lift an already-obtained capture outcome into a verified-against-reality verdict.

    Pure and offline: it maps a ``CallOutcome`` that ``_capture`` / a live call already
    produced — it does NOT call upstream. Status/shape only, never a response body
    (invariant #1); the outcome it reads is itself payload-free.

    Only a status that came off the wire (``source == "observed"``) can VERIFY or REFUTE a
    claim. Recorded/synthetic/probe runs fabricate their status, so per the milestone's
    honesty flag they stay UNVERIFIED — a recorded 200 is never overclaimed as VERIFIED.
    """
    if outcome.source != "observed":
        return VerifyVerdict(status="UNVERIFIED", basis=("no-access:recorded-only",))
    status = outcome.status
    if status is None:
        # Live-intent but the request never reached the wire: the claim's contract is
        # wrong (a missing/invalid required input caught pre-flight). The error_class is a
        # category name, never a value — control-plane safe to name in the basis.
        return VerifyVerdict(
            status="REFUTED", basis=(f"contract-mismatch:{outcome.error_class}",)
        )
    if 200 <= status < 400:
        return VerifyVerdict(
            status="VERIFIED", basis=(f"replay:{status}",), shape_ok=True
        )
    # 404 and every other 4xx/5xx refute the claimed operation.
    return VerifyVerdict(status="REFUTED", basis=(f"replay:{status}",))


def example_args(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema", {}) or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    return {
        name: example_from_schema(props[name]) for name in props if name in required
    }


def validate_tool(
    client: AgentApiClient,
    tool: dict[str, Any],
    corpus_path: str | Path | None = None,
) -> dict[str, Any]:
    args = example_args(tool)
    exc: CallError | None = None
    try:
        build_request(tool, args, client.base_url, client.session.auth_headers())
        result = {"tool": tool["name"], "ok": True, "reason": ""}
    except CallError as e:
        exc = e
        result = {"tool": tool["name"], "ok": False, "reason": str(e)}
    if corpus_path is not None:
        _capture(client, tool, args, exc, corpus_path)
    return result


def _capture(
    client: AgentApiClient,
    tool: dict[str, Any],
    args: dict[str, Any],
    exc: CallError | None,
    corpus_path: str | Path,
) -> None:
    """Append a control-plane-safe pre-flight outcome via the corpus boundary — metadata
    only (a validation run never calls upstream, so ``status`` is None). Structurally
    cannot persist a value: ``outcome_from`` has no parameter for a body or filled URL."""
    invoke = tool.get("_invoke")
    if not isinstance(invoke, dict):
        return
    corpus.record(
        corpus.outcome_from(
            operation_id=tool["name"],
            tool_invoke=invoke,
            args=args,
            status=None,
            error_class=corpus.error_class_for(None, exc),
            latency_ms=None,
            mode="recorded",
            auth_injected=False,
            ts=int(time.time() * 1000),
            surface_id=client.surface_id,
            surface_rev=client.surface_rev,
        ),
        corpus_path,
    )


def validate_all(
    client: AgentApiClient,
    log_path: str | None = None,
    corpus_path: str | Path | None = None,
) -> dict[str, Any]:
    results = [validate_tool(client, t, corpus_path) for t in client.list_tools()]
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "failed": [r for r in results if not r["ok"]],
        "results": results,
    }
