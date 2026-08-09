"""Arazzo export — the derived plan as a portable handoff artifact.

A **pure serializer**. It turns a :class:`gecko.graph.Plan` (or a safety-gated
:class:`gecko.safechain.SafeChainResult`) into an Arazzo 1.0 document any runtime can
execute. Approved 2026-07-29 in
``docs/specs/2026-07-29-arazzo-spdg-orchestration-plan.md``: *export now, ingest later,
never the executor.* Gecko emits the plan; the agent runtime executes it and holds the
values. No I/O, no network, no store, no execution — call it with normalized inputs, get
a ``dict`` back.

**Why export at all.** ``Plan``/``SafeChainResult`` are Python objects only our own CLI
and MCP can read, so the DAG we derive is invisible outside this process. Arazzo is the
OpenAPI Initiative's interchange format for exactly this shape (``steps[]`` with
``operationId``, parameters, ``outputs``), so serializing costs us nothing and buys
portability. Anyone can hand-write Arazzo; only Gecko *derives* it from an untrusted
spec, with per-edge provenance and the customer-confirmation gate attached.

**The one-way detail — a refused hop is NEVER a callable step.**
Arazzo 1.0's Step Object schema requires exactly one of
``operationId``/``operationPath``/``workflowId``. Every member of ``steps[]`` is, by
construction, an invocation: the format has **no shape for "this step is deliberately
not runnable"**. So we do not force one. A chain carrying a quarantined hop (or any hop
we cannot fully express) emits **no workflow at all** — the hops are recorded under the
root ``x-gecko-refusals`` extension, outside ``workflows``, and ``workflows`` is left
empty. That deliberately violates the spec's ``workflows: minItems 1``, so the document
does not load in a conformant runtime. Fail-closed by construction: the worst a runtime
can do with a refused export is nothing. Any encoding that *did* validate would hand an
executor a step-shaped object, and step-shaped means callable.

**No values, ever (invariant #1).** Every wire-bound leaf we emit is an Arazzo *runtime
expression* — ``$inputs.<name>`` for a caller-supplied input, ``$steps.<id>.outputs.<f>``
for a join, ``$response.body#/<f>`` for an output. Those are NAME references the runtime
resolves against data it holds. A literal here would put Gecko on the data plane and
trade away unilateral ingest — the exact line the 07-29 decision drew.

**Provenance rides on the edge.** A ``feeds`` edge lands on a Parameter Object (or, for a
body-carried join, on ``requestBody``); its ``x-gecko-provenance`` says class, basis,
confidence, the supplier step/field, and — for a cross-API join — the ``gate`` that
``compose.cross_plan`` enforced. Arazzo permits ``x-`` extensions on both objects, so the
provenance travels *with* the edge rather than in a side-car a consumer can drop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from .graph import ExplainEntry, Plan, PlanStep, SurfaceGraph
from .safechain import SafeChainResult

#: The Arazzo Specification version this serializer targets. 1.0.1 is the current 1.0.x
#: patch; the vendored OAI schema (``tests/fixtures/arazzo/``) covers all of 1.0.x.
ARAZZO_VERSION = "1.0.1"

#: Why a workflow was withheld. NOT a provenance ladder and NOT a trust classification —
#: it never says where a fact came from, only why nothing runnable was emitted. Adding a
#: kind here needs no anti-poisoning review; adding a value to ``gecko.provenance`` does.
RefusalKind = Literal[
    "no-confident-plan",  # the planner returned None (e.g. the cross-API gate refused)
    "quarantined-hop",  # Skill Guard quarantined a hop on the chain
    "unresolved-parameter-location",  # Arazzo needs `in`; we could not recover it
    # The join key lives inside a collection. Arazzo 1.0 has no `for-each` and no
    # "which one" expression, so there is NO correct pointer to emit — `/0/` would not
    # fix the ambiguity, it would PROMOTE it from the control plane (where it moves a
    # score) into a published document a runtime acts on. Refusing is the honest
    # emission, and it is why the destructive-verb question never had to be answered:
    # the element-0 bindings are refused on arity before anyone rules on the verb.
    "unresolved-output-arity",
]

_REFUSAL_NOTICE = (
    "This document is NOT executable and does NOT validate against the Arazzo 1.0 "
    "schema: `workflows` is empty, violating `minItems: 1`. That is deliberate. Arazzo "
    "has no non-runnable step — every member of `steps[]` MUST carry an operationId, "
    "operationPath or workflowId — so a hop Gecko refused cannot be encoded as a step "
    "without becoming callable. See `x-gecko-refusals` for what was withheld and why."
)

_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")  # sourceDescriptions[].name pattern
_EXPR_SAFE = re.compile(r"[^A-Za-z0-9._-]")  # outputs/inputs key pattern
_PATH_TEMPLATE = re.compile(r"\{([^{}/]+)\}")
#: Locations Arazzo's Parameter Object can express. `body` is NOT one of them (it goes to
#: requestBody); `header`/`cookie` never reach a plan (graph drops auth-shaped inputs).
_PARAM_IN = ("path", "query", "header", "cookie")
#: Deterministic precedence when one op declares the same name in two locations.
_LOCATION_ORDER = ("path", "query", "body", "header", "cookie")

__all__ = ["ARAZZO_VERSION", "RefusalKind", "is_executable", "to_arazzo"]


def to_arazzo(
    result: Plan | SafeChainResult | None,
    *,
    graphs: Iterable[SurfaceGraph] = (),
    sources: Mapping[str, str] | None = None,
    title: str = "Gecko derived plan",
    doc_version: str = "1.0.0",
    workflow_id: str = "gecko-plan",
    summary: str | None = None,
    refusal_reason: str = "",
) -> dict[str, Any]:
    """Serialize a derived plan as an Arazzo 1.0 document. Pure: no I/O, no values.

    ``result`` is a :class:`~gecko.graph.Plan`, a :class:`~gecko.safechain.SafeChainResult`
    (whose quarantine stance is honored), or ``None`` — the planner's honest "no confident
    plan", which serializes as a non-executable refusal document rather than vanishing.

    ``graphs`` are the surfaces the plan was derived over. They are what makes each
    parameter's ``in`` recoverable; without them a workflow whose parameters we cannot
    place is refused rather than guessed. ``sources`` maps ``surface_id -> spec URL``; an
    unknown URL is marked ``x-gecko-source-url: "unknown"``, never invented.

    Use :func:`is_executable` on the result — a refused document carries no workflow and
    does not validate against the Arazzo schema (see the module docstring).
    """
    plan, refusals, withheld = _unwrap(result, refusal_reason)
    step_index = _step_ids(plan or withheld)
    locations = _locations(graphs)
    sigs = _signatures(graphs)
    pointers = _pointers(graphs)
    src_names = _source_names(plan or withheld)

    steps: list[dict[str, Any]] = []
    caller_inputs: dict[str, str] = {}
    if plan is not None:
        for pstep in plan.steps:
            step, unresolved, needed, unresolved_outputs = _step(
                pstep,
                plan,
                pointers,
                step_index,
                src_names,
                locations,
                qualify=len(src_names) > 1,
            )
            caller_inputs.update(needed)
            for name in unresolved:
                refusals.append(
                    {
                        "kind": "unresolved-parameter-location",
                        "surfaceId": pstep.surface,
                        "operationId": pstep.operation_id,
                        "parameter": name,
                        "emittedAsStep": False,
                        "reason": (
                            "Arazzo requires `in` on an operation-bound parameter and the "
                            "location was not recoverable from the supplied graphs or the "
                            "path template. Guessing would silently misplace the input."
                        ),
                    }
                )
            for field_name, why in unresolved_outputs:
                refusals.append(
                    {
                        "kind": "unresolved-output-arity",
                        "surfaceId": pstep.surface,
                        "operationId": pstep.operation_id,
                        "field": field_name,
                        "emittedAsStep": False,
                        "reason": (
                            "the value lives inside a collection and Arazzo 1.0 has no "
                            "`for-each` and no which-one expression, so no correct "
                            "pointer exists; emitting `/0/` would bind whichever "
                            "element sorted first and call it verified"
                            if why == "many"
                            else "the field's location in the response body was never "
                            "established; the previous behaviour guessed the body root, "
                            "which produced a plausible wrong pointer"
                        ),
                    }
                )
            steps.append(step)

    executable = plan is not None and not refusals and bool(steps)
    doc: dict[str, Any] = {
        "arazzo": ARAZZO_VERSION,
        "info": {
            "title": title,
            "version": doc_version,
            "summary": summary or _summary(result),
        },
        "sourceDescriptions": _source_descriptions(src_names, sources or {}),
        "workflows": [],
        "x-gecko-executable": executable,
    }
    if executable:
        doc["workflows"] = [
            {
                "workflowId": _slug(workflow_id) or "gecko-plan",
                "summary": doc["info"]["summary"],
                "inputs": _inputs_schema(caller_inputs, sigs),
                "steps": steps,
            }
        ]
    else:
        doc["x-gecko-refusals"] = refusals or [
            {
                "kind": "no-confident-plan",
                "emittedAsStep": False,
                "reason": refusal_reason or "the planner returned no confident plan",
            }
        ]
        # The WHOLE chain is withheld, not just the bad hop. Naming the clean hops here
        # is the honest gap (invariant #3): a reader can see exactly what was dropped and
        # cannot mistake a surviving prefix for a partial plan we endorsed.
        if withheld is not None:
            doc["x-gecko-withheld"] = {
                "workflowId": _slug(workflow_id) or "gecko-plan",
                "hops": [
                    {
                        "stepId": step_index[(s.surface, s.operation_id)],
                        "surfaceId": s.surface,
                        "operationId": s.operation_id,
                        "refused": any(
                            r.get("surfaceId") == s.surface
                            and r.get("operationId") == s.operation_id
                            for r in refusals
                        ),
                    }
                    for s in withheld.steps
                ],
            }
        doc["x-gecko-refusal-notice"] = _REFUSAL_NOTICE
    return doc


def is_executable(doc: Mapping[str, Any]) -> bool:
    """True iff the document carries a runnable workflow (i.e. nothing was refused)."""
    return bool(doc.get("x-gecko-executable")) and bool(doc.get("workflows"))


# --- unwrapping the two input shapes ---------------------------------------------
def _unwrap(
    result: Plan | SafeChainResult | None, refusal_reason: str
) -> tuple[Plan | None, list[dict[str, Any]], Plan | None]:
    """``(plan, refusals, withheld)``.

    THE one-way gate. A quarantined hop becomes a refusal record *here* and never reaches
    ``_step``, so there is exactly one place in this module where a hop could become a
    callable step and it is unreachable from a refused chain. ``withheld`` is the plan we
    did NOT emit — carried only so the refusal can name every hop it dropped.
    """
    if result is None:
        return (
            None,
            [
                {
                    "kind": "no-confident-plan",
                    "emittedAsStep": False,
                    "reason": refusal_reason
                    or "the planner returned no confident plan",
                }
            ],
            None,
        )
    if isinstance(result, SafeChainResult):
        refusals = [
            {
                "kind": "quarantined-hop",
                "surfaceId": node.surface_id,
                "operationId": node.operation_id,
                "tool": node.tool,
                "emittedAsStep": False,
                "reason": node.quarantine_reason or "Skill Guard: quarantined",
            }
            for node in result.quarantined_nodes
        ]
        if result.plan is None and not refusals:
            refusals.append(
                {
                    "kind": "no-confident-plan",
                    "emittedAsStep": False,
                    "reason": refusal_reason
                    or "the safety-gated chain carries no serializable plan",
                }
            )
        if refusals:
            return None, refusals, result.plan
        return result.plan, [], None
    return result, [], None


def _summary(result: Plan | SafeChainResult | None) -> str:
    if isinstance(result, SafeChainResult):
        return result.summary
    if result is None:
        return "No confident plan — nothing runnable was derived."
    return f"Deterministic {len(result.steps)}-step plan derived by Gecko."


# --- ids ---------------------------------------------------------------------------
def _slug(text: str) -> str:
    return _ID_SAFE.sub("-", text).strip("-")


def _expr_key(text: str) -> str:
    return _EXPR_SAFE.sub("-", text).strip("-") or "value"


def _source_names(plan: Plan | None) -> dict[str, str]:
    """surface_id -> the Arazzo sourceDescriptions name (pattern ``[A-Za-z0-9_-]+``),
    deduplicated deterministically."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for sid in _ordered({s.surface for s in (plan.steps if plan else ())}):
        base = _slug(sid) or "surface"
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        taken.add(name)
        out[sid] = name
    return out


def _step_ids(plan: Plan | None) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    taken: set[str] = set()
    for pstep in plan.steps if plan else ():
        base = _slug(f"{pstep.surface}-{pstep.operation_id}") or "step"
        sid, n = base, 2
        while sid in taken:
            sid, n = f"{base}-{n}", n + 1
        taken.add(sid)
        out[(pstep.surface, pstep.operation_id)] = sid
    return out


def _ordered(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


# --- graph lookups (the honest source of `in` and of input types) ------------------
def _pointers(
    graphs: Iterable[SurfaceGraph],
) -> dict[tuple[str, str, str], tuple[str, str]]:
    """(surface_id, operation_id, lowercased field name) -> (source_pointer, arity).

    Read off the graph's FIELD nodes, mirroring :func:`_locations` for params. The
    producer side is symmetric with the consumer side on purpose: neither fact belongs on
    ``ExplainEntry``, which is a per-plan projection that one of its two construction
    sites cannot compute.
    """
    out: dict[tuple[str, str, str], tuple[str, str]] = {}
    for graph in graphs:
        for node in graph.nodes:
            if node.kind != "field":
                continue
            out.setdefault(
                (graph.surface_id, node.owner, node.name.lower()),
                (node.source_pointer, node.arity),
            )
    return out


def _locations(graphs: Iterable[SurfaceGraph]) -> dict[tuple[str, str, str], str]:
    """(surface_id, operation_id, lowercased param name) -> location, read off the graph's
    param nodes (``detail`` is ``"{location}|{req|opt}"``). One name may exist in two
    locations on one op; ``_LOCATION_ORDER`` picks deterministically."""
    out: dict[tuple[str, str, str], str] = {}
    for graph in graphs:
        for node in graph.nodes:
            if node.kind != "param" or not node.detail:
                continue
            loc = node.detail.split("|", 1)[0]
            key = (graph.surface_id, node.owner, node.name.lower())
            prev = out.get(key)
            if prev is None or _rank(loc) < _rank(prev):
                out[key] = loc
    return out


def _rank(location: str) -> int:
    return (
        _LOCATION_ORDER.index(location)
        if location in _LOCATION_ORDER
        else len(_LOCATION_ORDER)
    )


def _signatures(graphs: Iterable[SurfaceGraph]) -> dict[tuple[str, str, str], str]:
    """(surface_id, operation_id, lowercased name) -> the raw JSON type from the §13.1
    value-domain signature. Type only — never a format-derived example, never a value."""
    out: dict[tuple[str, str, str], str] = {}
    for graph in graphs:
        for node in graph.nodes:
            if node.kind != "param" or not node.sig:
                continue
            out.setdefault(
                (graph.surface_id, node.owner, node.name.lower()),
                node.sig.split("|", 1)[0],
            )
    return out


# --- steps -------------------------------------------------------------------------
def _step(
    pstep: PlanStep,
    plan: Plan,
    pointers: dict[tuple[str, str, str], tuple[str, str]],
    step_index: Mapping[tuple[str, str], str],
    src_names: Mapping[str, str],
    locations: Mapping[tuple[str, str, str], str],
    *,
    qualify: bool,
) -> tuple[dict[str, Any], list[str], dict[str, str], list[tuple[str, str]]]:
    """One PlanStep -> one Arazzo Step Object.

    Returns ``(step, unresolved_parameter_names, caller_supplied_inputs)``. Every consumed
    input gets a runtime expression: the supplier's output when a ``feeds`` edge explains
    it, otherwise ``$inputs.<name>`` (declared on the workflow, supplied by the caller).
    """
    source = src_names[pstep.surface]
    op_ref = (
        f"$sourceDescriptions.{source}.{pstep.operation_id}"
        if qualify
        else pstep.operation_id
    )
    step: dict[str, Any] = {
        "stepId": step_index[(pstep.surface, pstep.operation_id)],
        "description": f"{pstep.method.upper()} {pstep.path}",
        "operationId": op_ref,
        # the un-mangled identity, so a consumer can walk back to our graph
        "x-gecko-surface-id": pstep.surface,
        "x-gecko-operation-id": pstep.operation_id,
    }

    path_params = set(_PATH_TEMPLATE.findall(pstep.path))
    parameters: list[dict[str, Any]] = []
    body: dict[str, Any] = {}
    body_prov: dict[str, Any] = {}
    unresolved: list[str] = []
    caller_inputs: dict[str, str] = {}

    for name in pstep.consumes:
        edge = _edge_for(plan, pstep, name)
        value = _expression(edge, step_index, name, pstep.surface)
        if edge is None:
            caller_inputs[_expr_key(name)] = name
        location = _location_of(pstep, name, path_params, locations)
        if location == "body":
            body[name] = value
            if edge is not None:
                body_prov[name] = _provenance(edge, step_index, pstep.surface)
            continue
        if location not in _PARAM_IN:
            unresolved.append(name)
            continue
        param: dict[str, Any] = {"name": name, "in": location, "value": value}
        if edge is not None:
            param["x-gecko-provenance"] = _provenance(edge, step_index, pstep.surface)
        parameters.append(param)

    if parameters:
        step["parameters"] = parameters
    if body:
        # §13.5 mutate-chain: a body-carried join key has no Parameter Object to ride, so
        # the join lands in requestBody and its provenance rides there, keyed by field.
        request_body: dict[str, Any] = {
            "contentType": "application/json",
            "payload": body,
        }
        if body_prov:
            request_body["x-gecko-provenance"] = body_prov
        step["requestBody"] = request_body

    outputs, unresolved_outputs = _outputs(plan, pstep, pointers)
    if outputs:
        step["outputs"] = outputs
    return step, unresolved, caller_inputs, unresolved_outputs


def _location_of(
    pstep: PlanStep,
    name: str,
    path_params: set[str],
    locations: Mapping[tuple[str, str, str], str],
) -> str:
    """The parameter's location, or ``""`` when it is not recoverable. The path template
    on the step is authoritative for path params; otherwise ask the graph. NEVER default —
    a guessed location silently sends a path id as a query arg."""
    if name in path_params:
        return "path"
    return locations.get((pstep.surface, pstep.operation_id, name.lower()), "")


def _edge_for(plan: Plan, pstep: PlanStep, name: str) -> ExplainEntry | None:
    """The ``feeds`` edge that satisfies ``name`` on this step, if the plan explains it.
    Matched on the param name and on the supplier actually being a step in this plan —
    a dangling explain entry must not mint a ``$steps.…`` reference to nothing."""
    ops = {(s.surface, s.operation_id) for s in plan.steps}
    for entry in plan.explain:
        if entry.param != name:
            continue
        surface = entry.source_surface or pstep.surface
        if (surface, entry.source_op) in ops and entry.source_op != pstep.operation_id:
            return entry
    return None


def _expression(
    edge: ExplainEntry | None,
    step_index: Mapping[tuple[str, str], str],
    name: str,
    consumer_surface: str,
) -> str:
    """A runtime EXPRESSION — never a value. This is the whole control-plane guarantee.

    The supplier is looked up on ``(surface, operation)``, never on the operation alone:
    two surfaces in one workspace may share an ``operationId``, and binding to the wrong
    one is precisely the wrong-but-plausible edge this codebase exists to prevent.
    """
    if edge is None:
        return f"$inputs.{_expr_key(name)}"
    key = (edge.source_surface or consumer_surface, edge.source_op)
    return f"$steps.{step_index[key]}.outputs.{_expr_key(edge.source_field)}"


def _provenance(
    edge: ExplainEntry,
    step_index: Mapping[tuple[str, str], str],
    consumer_surface: str,
) -> dict[str, Any]:
    """The per-edge provenance record. Class + basis + confidence + where it came from —
    and, for a cross-surface join, the gate ``compose.cross_plan`` enforced to admit it.

    ``provenance`` is the closed ladder from :mod:`gecko.provenance`, passed through
    verbatim. It is never widened here and a ``feeds`` edge is never EXTRACTED.
    """
    source_surface = edge.source_surface or consumer_surface
    cross = bool(edge.source_surface) and edge.source_surface != consumer_surface
    record: dict[str, Any] = {
        "provenance": edge.provenance,
        "basis": edge.basis,
        "confidence": edge.confidence,
        "sourceSurfaceId": source_surface,
        "sourceOperationId": edge.source_op,
        "sourceField": edge.source_field,
        "sourceStepId": step_index.get((source_surface, edge.source_op), ""),
        "crossApi": cross,
    }
    if cross:
        # compose.cross_plan admits a cross-surface join ONLY when BOTH sides map to the
        # same entity in their CUSTOMER-CONFIRMED vocabulary. Stating it on the edge is
        # what stops a consumer reading this as "two names matched".
        record["gate"] = "customer-confirmed"
    return record


def _outputs(
    plan: Plan, pstep: PlanStep, pointers: dict[tuple[str, str, str], tuple[str, str]]
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """The response fields LATER steps consume, as runtime expressions. Only fields the
    plan actually joins on are named — this is not a response schema dump, and it carries
    no value.

    The pointer comes from the graph's field node, not from a guess at the body root.
    Returns ``(outputs, unresolved)`` — ``unresolved`` names fields whose location or
    arity could not be honestly expressed, so the caller refuses instead of emitting.

    Two refusal cases, both deliberate:

    * **no pointer** — the field's location was never established. The previous code
      emitted ``#/<name>`` regardless, which produced a plausible wrong pointer.
    * **arity == "many"** — the value lives inside a collection, and Arazzo 1.0 has no
      ``for-each`` and no "which one" expression. There is no correct pointer to emit.
      Emitting ``/0/`` would move the element-0 ambiguity out of the control plane and
      into a document a runtime acts on, stamped "verified resolvable".
    """
    out: dict[str, str] = {}
    unresolved: list[tuple[str, str]] = []
    for entry in plan.explain:
        if entry.source_op != pstep.operation_id:
            continue
        if entry.source_surface and entry.source_surface != pstep.surface:
            continue
        pointer, arity = pointers.get(
            (pstep.surface, pstep.operation_id, entry.source_field.lower()),
            ("", "unknown"),
        )
        if arity == "many":
            unresolved.append((entry.source_field, "many"))
            continue
        if not pointer:
            unresolved.append((entry.source_field, "unestablished"))
            continue
        out[_expr_key(entry.source_field)] = f"$response.body#{pointer}"
    return dict(sorted(out.items())), unresolved


# --- document scaffolding ----------------------------------------------------------
def _source_descriptions(
    src_names: Mapping[str, str], sources: Mapping[str, str]
) -> list[dict[str, Any]]:
    """One entry per surface. ``url`` is REQUIRED by Arazzo; when the caller did not give
    us the spec's location we emit a relative reference and mark it ``unknown`` rather
    than inventing a URL that resolves to someone else's document."""
    out: list[dict[str, Any]] = []
    for surface_id, name in src_names.items():
        url = sources.get(surface_id, "")
        out.append(
            {
                "name": name,
                "url": url or f"{name}.openapi.json",
                "type": "openapi",
                "x-gecko-surface-id": surface_id,
                "x-gecko-source-url": "declared" if url else "unknown",
            }
        )
    return out


def _inputs_schema(
    caller_inputs: Mapping[str, str],
    sigs: Mapping[tuple[str, str, str], str],
) -> dict[str, Any]:
    """A JSON Schema naming the inputs the CALLER must supply — names and raw JSON types
    only. No example, no default, no enum: those are values."""
    properties: dict[str, Any] = {}
    for key in sorted(caller_inputs):
        name = caller_inputs[key]
        raw = next(
            (t for (_s, _o, n), t in sorted(sigs.items()) if n == name.lower()), ""
        )
        properties[key] = {"type": raw} if raw in _JSON_TYPES else {}
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
    }


_JSON_TYPES: Sequence[str] = (
    "string",
    "integer",
    "number",
    "boolean",
    "array",
    "object",
)
