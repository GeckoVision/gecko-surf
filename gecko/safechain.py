"""Safe cross-API chain composition — run a DECLARED-confirmed chain while dropping
any node a surface has quarantined (Skill Guard), so a poisoned provider can never
steer a financial answer.

This is the Phase-2 "safety of the chain" composition. It is PURE COMPOSITION over
shipped engines — it invents no planner, no detector, no transport:

    the chain             ``compose.cross_plan``   (the DECLARED-confirmed join)
    the safety verdict    ``surface.Surface.safety`` (per-tool Skill-Guard quarantine)
    the agent-facing name  ``tools.tool_name``       (op -> the name the agent sees)

The value it adds is a single honest rule applied to a composed plan: **never proceed
THROUGH a quarantined node.** A financial supplier chain is only as safe as its hops —
so if any hop's surface has quarantined that op (a poisoned tool description / doc image
tripped the anti-poisoning sanitizer on ingest), the chain REFUSES at that hop with a
provenance reason rather than calling it. The nodes that already resolved cleanly still
contribute their provenance to the answer; the poisoned node never does, and the agent
is never handed a plan that walks into it.

Control-plane only (invariant #1): this composes surfaces + plan metadata + the safety
verdict. It performs NO transport and returns NO response payload — a
:class:`SafeChainResult` is planning + provenance + safety, never data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .compose import Workspace, cross_plan
from .surface import SafetyVerdict, Surface
from .tools import tool_name


@dataclass(frozen=True)
class ChainNode:
    """One hop of a composed chain, tagged with its safety stance.

    ``quarantined`` is read from the owning surface's Skill-Guard verdict; a quarantined
    node is NEVER called and contributes no data — ``quarantine_reason`` names the rules
    that tripped (the auditable "why"), so the refusal is legible, not a black box.
    """

    surface_id: str
    operation_id: str
    tool: str
    quarantined: bool
    quarantine_reason: str | None


@dataclass(frozen=True)
class SafeChainResult:
    """The provenance-tagged verdict of a safety-gated cross-API chain.

    Either the chain ``complete``s (every hop clean, answer grounded) or it is a clean
    ``refused`` (a hop was quarantined, so the chain stopped there). It NEVER proceeds
    through a poisoned node — that is the guarantee ``safe`` records.
    """

    target_surface: str
    target_tool: str
    nodes: tuple[ChainNode, ...]
    join_basis: tuple[str, ...]  # the DECLARED cross-edge bases, e.g. ("declared:…",)
    complete: bool  # every hop resolved clean -> the chain answered
    refused: bool  # a hop was quarantined -> a clean, honest refusal
    summary: str  # grounded, provenance-tagged, human-readable

    @property
    def quarantined_nodes(self) -> tuple[ChainNode, ...]:
        return tuple(n for n in self.nodes if n.quarantined)

    @property
    def safe(self) -> bool:
        """The invariant made explicit: no quarantined node ever drove the answer.

        True by construction — a quarantined hop is dropped, never called — but stated
        as a property so a caller (a test, a policy gate) can assert the guarantee held.
        """
        return not any(n.quarantined for n in self.nodes) or self.refused


def _quarantine_reason(verdict: SafetyVerdict, tool: str) -> str:
    """The auditable "why" for a quarantined node — READ from the surface's safety verdict,
    not recomputed. The sanitizer already recorded the tripped rule categories at ingest
    (``x-poison-reason`` -> ``SafetyVerdict.reasons``); this just formats them. A tool
    quarantined for a schema/response reason (no captured category) falls back to a generic
    reason, matching the pre-refactor behavior — with NO second scan of the spec text."""
    category = verdict.reasons.get(tool)
    if not category:
        return "Skill Guard: quarantined"
    return "Skill Guard: " + category


def compose_safe_chain(
    surfaces: Mapping[str, Surface],
    target_surface: str,
    target_op: str,
    satisfiable: Iterable[str] = (),
) -> SafeChainResult | None:
    """Compose a safety-gated chain for ``target_op`` on ``target_surface``.

    Plans the (possibly cross-surface) chain with the shipped ``cross_plan`` over the
    given surfaces' graphs, then walks the plan applying ONE rule: never proceed through
    a node its surface has quarantined. Returns ``None`` when no confident plan exists
    (the same honest "no plan" ``cross_plan`` returns) — never a speculative join.

    ``surfaces`` maps ``surface_id -> Surface``; ``target_op`` is the intent operation's
    ``operationId``. ``satisfiable`` are inputs already known from the intent.
    """
    workspace = Workspace(graphs=tuple(s.graph for s in surfaces.values()))
    plan = cross_plan(workspace, target_surface, target_op, set(satisfiable))
    if plan is None:
        return None

    nodes: list[ChainNode] = []
    for step in plan.steps:
        surface = surfaces[step.surface]
        verdict = (
            surface.safety
        )  # the carried verdict: quarantine set + reasons, no re-scan
        tname = _tool_for(surface, step.operation_id)
        quarantined = tname in verdict.quarantined
        nodes.append(
            ChainNode(
                surface_id=step.surface,
                operation_id=step.operation_id,
                tool=tname,
                quarantined=quarantined,
                quarantine_reason=(
                    _quarantine_reason(verdict, tname) if quarantined else None
                ),
            )
        )

    join_basis = tuple(
        sorted({e.basis for e in plan.explain if e.provenance == "DECLARED"})
    )
    target_tool = _tool_for(surfaces[target_surface], target_op)
    refused = any(n.quarantined for n in nodes)
    complete = not refused
    return SafeChainResult(
        target_surface=target_surface,
        target_tool=target_tool,
        nodes=tuple(nodes),
        join_basis=join_basis,
        complete=complete,
        refused=refused,
        summary=_summarize(target_surface, target_tool, nodes, join_basis, refused),
    )


def _tool_for(surface: Surface, operation_id: str) -> str:
    op = next(
        (o for o in surface.client.operations if o.operation_id == operation_id), None
    )
    return tool_name(op) if op is not None else operation_id


def _summarize(
    target_surface: str,
    target_tool: str,
    nodes: list[ChainNode],
    join_basis: tuple[str, ...],
    refused: bool,
) -> str:
    """A grounded, provenance-tagged one-liner. Names the hops, the confirmed join basis,
    and — on refusal — the quarantined hop and its reason. Carries no response payload."""
    hops = " → ".join(f"{n.surface_id}.{n.tool}" for n in nodes)
    basis = ", ".join(join_basis) or "intra-surface"
    if refused:
        bad = next(n for n in nodes if n.quarantined)
        return (
            f"REFUSED: {hops} — join {basis} (customer-confirmed), but the hop "
            f"{bad.surface_id}.{bad.tool} is quarantined ({bad.quarantine_reason}). "
            f"The chain does not proceed through a poisoned node; the poisoned "
            f"instruction never reached the agent."
        )
    return (
        f"CHAIN: {hops} → {target_surface}.{target_tool} — join {basis} "
        f"(customer-confirmed, DECLARED). All hops clean; no auth header exposed."
    )


__all__ = ["ChainNode", "SafeChainResult", "compose_safe_chain"]
