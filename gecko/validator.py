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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import corpus
from .caller import CallError, build_request
from .capture import record_outcome
from .client import AgentApiClient
from .graph import VerifyVerdict
from .sample import example_from_schema, example_is_grounded

#: The only statuses that are EVIDENCE the documented operation is not there: the path is
#: not served (404), the documented method is not served on it (405), or it was withdrawn
#: (410). Every other error status proves the OPPOSITE — the server routed the request and
#: then complained about it (400/422 = our arguments, 401/403 = access, 429 = rate, 5xx =
#: the API broke) — so none of them may refute an endpoint's existence.
_ABSENCE_STATUSES = frozenset({404, 405, 410})

#: Pre-flight failures that are genuinely a broken CONTRACT (the documented inputs cannot
#: be satisfied), as opposed to Gecko declining to make the call (``auth_host_blocked``) or
#: the network failing (``timeout``) — those say nothing about the provider's docs.
_CONTRACT_ERROR_CLASSES = frozenset(
    {"missing_required_param", "malformed_request", "enum_reject"}
)

#: Where an argument participates in identifying the RESOURCE being addressed. A
#: synthesized header/cookie/body value cannot be why the API answered "no such thing", so
#: only these two locations gate the REFUTED verdict.
_ROUTE_LOCATIONS = frozenset({"path", "query"})


def synthesized_route_args(tool: Mapping[str, Any]) -> tuple[str, ...]:
    """The required path/query params of ``tool`` that :func:`example_args` fills with a
    placeholder WE invented (never one the spec or the value-domain registry declared).

    These are the args that make an error status un-attributable to the endpoint: the call
    asked for an entity that was never real. Returns sorted param NAMES only — control
    plane, never a value (invariant #1).
    """
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    invoke = tool.get("_invoke") or {}
    locations = invoke.get("param_locations") or {}
    # the agent-facing key may be a sanitized alias (e.g. JSON:API ``filter[user]``);
    # param_locations is keyed on the REAL name, so translate before looking it up.
    aliases = invoke.get("arg_aliases") or {}
    ungrounded: list[str] = []
    for key in props:
        if key == "body" or key not in required:
            continue
        real = str(aliases.get(key, key))
        if locations.get(real) not in _ROUTE_LOCATIONS:
            continue
        if not example_is_grounded(props[key]):
            ungrounded.append(real)
    return tuple(sorted(ungrounded))


def verdict_from_replay(
    outcome: corpus.CallOutcome, *, synthesized_args: Sequence[str] = ()
) -> VerifyVerdict:
    """Lift an already-obtained capture outcome into a verified-against-reality verdict.

    Pure and offline: it maps a ``CallOutcome`` that ``_capture`` / a live call already
    produced — it does NOT call upstream. Status/shape only, never a response body
    (invariant #1); the outcome it reads is itself payload-free.

    Only a status that came off the wire (``source == "observed"``) can VERIFY or REFUTE a
    claim. Recorded/synthetic/probe runs fabricate their status, so per the milestone's
    honesty flag they stay UNVERIFIED — a recorded 200 is never overclaimed as VERIFIED.

    **REFUTED means "this endpoint is not there", and nothing else.** That verdict names a
    real company's API as broken, so it is issued only on evidence of ABSENCE
    (:data:`_ABSENCE_STATUSES`) AND only when nothing about the call was made up:
    ``synthesized_args`` (from :func:`synthesized_route_args`) lists the required path/query
    params we filled with an invented placeholder, and any of them turns the verdict into
    UNVERIFIED with a basis naming the param we could not ground. A 404 on
    ``/assets/sample`` is not a missing endpoint — it is a missing *asset*. Under-claiming
    is deliberate here: a missed refutation costs us a data point, a false one defames a
    partner.
    """
    if outcome.source != "observed":
        return VerifyVerdict(status="UNVERIFIED", basis=("no-access:recorded-only",))
    status = outcome.status
    if status is None:
        # Live-intent but the request never reached the wire. Only a genuine contract
        # failure (the documented inputs cannot be satisfied) refutes; a refusal on OUR
        # side or a transport failure is honestly "not checked". The error_class is a
        # category name, never a value — control-plane safe to name in the basis.
        if outcome.error_class in _CONTRACT_ERROR_CLASSES:
            return VerifyVerdict(
                status="REFUTED", basis=(f"contract-mismatch:{outcome.error_class}",)
            )
        return VerifyVerdict(
            status="UNVERIFIED", basis=(f"no-access:{outcome.error_class}",)
        )
    if 200 <= status < 400:
        return VerifyVerdict(
            status="VERIFIED", basis=(f"replay:{status}",), shape_ok=True
        )
    if synthesized_args:
        # We could not verify: the call named an entity we invented, so the API's refusal
        # is about the ARGUMENT, not the endpoint. The basis says exactly which param.
        return VerifyVerdict(
            status="UNVERIFIED",
            basis=(
                f"replay:{status}",
                *(f"no-real-argument:{name}" for name in synthesized_args),
            ),
        )
    if status in _ABSENCE_STATUSES:
        return VerifyVerdict(status="REFUTED", basis=(f"replay:{status}",))
    # The endpoint answered — it exists, we just learned nothing about the claim.
    return VerifyVerdict(
        status="UNVERIFIED", basis=(f"replay:{status}", "no-evidence:endpoint-answered")
    )


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
    # Through the same fail-closed boundary the client/server use: a secret-shaped
    # operation_id/path/arg name in a poisoned spec REFUSES that one row (counted in
    # dropped.jsonl) instead of aborting the whole validation sweep.
    record_outcome(
        lambda: corpus.outcome_from(
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
        corpus_path=corpus_path,
        surface_id=client.surface_id,
        source=corpus.source_for_mode("recorded"),
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
