"""Derive the workflows an agent will actually want, ranked — no human authoring them.

A provider brings an API reference. Arazzo can express the workflows, and
``gecko export-arazzo`` already emits one — but it takes ``--op``, which means the
provider must already know which workflow they want. That is the part this removes.

A **candidate** is a producer→consumer hop the surface can satisfy on its own: an
operation callable with no context, whose output feeds an operation that needs one.
``get-defi-v2-tokens-new_listing`` returns an ``address``; ``get-defi-price`` needs one.
Nobody wrote that down. The graph knows it.

Candidates are plentiful (37 on Birdeye, 29 on Pegana), and a list of 37 is not a
deliverable — it is another triage job, which is the problem we are solving. So they are
**ranked**, on evidence already in the graph:

* **spine weight** — a hop through a handle most of the API depends on (``address``: 44
  of 89 operations) is worth more than one through a field used twice. This is the
  dominant term because it is the closest thing the graph has to "what this API is for".
* **hub bonus** — a join both produced and consumed here is chainable by construction;
  an input-only name is pagination.
* **floor penalty** — a target that ALSO needs a value nothing here produces is still
  emitted, but ranked below one that is self-contained, and the gap is named. Silently
  dropping it would hide the most interesting fact about the API.

One workflow per target operation: the best producer wins, so a provider gets N distinct
things their API can do rather than the same call reached six ways.

Control plane: names and counts only. Nothing here reads a response or holds a value.
Deterministic: same surface in, same ranking out, no model call.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .graph import SurfaceGraph
from .surfacereport import _short

#: Weight applied to a join that is both produced and consumed in this surface. A hub is
#: chainable by construction; an input-only name of the same reach is plumbing.
_HUB_BONUS = 2.0

#: Subtracted when the target ALSO needs an input nothing here produces. Enough to rank
#: a self-contained workflow first, never enough to hide one — the gap is the finding.
_FLOOR_PENALTY = 3.0


@dataclass(frozen=True)
class Candidate:
    """One derivable workflow: an entry operation whose output feeds a target."""

    producer: str
    target: str
    join: str
    #: Operations touching the join name — the spine reach that drives the rank.
    reach: int
    is_hub: bool
    #: Inputs the target needs that NO operation in this surface produces. The caller
    #: must supply these; a workflow that hides them is a workflow that fails at run time.
    caller_must_supply: tuple[str, ...]
    score: float

    @property
    def self_contained(self) -> bool:
        """True when the surface can satisfy every input of the target itself."""
        return not self.caller_must_supply


def derive_candidates(graph: SurfaceGraph, limit: int = 7) -> list[Candidate]:
    """Rank the workflows this surface can offer an agent. Pure — no I/O, no model.

    ``limit`` caps what a caller receives, not what is considered: ranking happens over
    every candidate, so the top N is the top of the real field rather than of whatever
    the graph happened to yield first.
    """
    ops = [n for n in graph.nodes if n.kind == "operation"]
    op_names = {n.name for n in ops}

    produced: set[str] = set()
    consumed: set[str] = set()
    reach: dict[str, set[str]] = defaultdict(set)
    for node in graph.nodes:
        if node.kind not in ("param", "field"):
            continue
        owner = node.owner if node.owner in op_names else _short(node.id)
        reach[node.name].add(owner)
        (produced if node.kind == "field" else consumed).add(node.name)

    required: dict[str, list[str]] = {}
    for op in ops:
        try:
            required[op.name] = [n.name for n in graph.required_inputs(op.name)]
        except Exception:
            required[op.name] = []
    entries = {name for name, need in required.items() if not need}

    best: dict[str, Candidate] = {}
    for edge in graph.edges:
        if edge.kind != "feeds":
            continue
        producer, target = _short(edge.src), _short(edge.dst)
        if producer == target or producer not in entries or target in entries:
            continue
        if target not in op_names:
            continue
        join = edge.dst.rsplit(":", 1)[-1]
        missing = tuple(
            sorted(n for n in required.get(target, []) if n not in produced)
        )
        is_hub = join in produced and join in consumed
        score = (
            float(len(reach.get(join, ())))
            + (_HUB_BONUS if is_hub else 0.0)
            - (_FLOOR_PENALTY if missing else 0.0)
        )
        candidate = Candidate(
            producer=producer,
            target=target,
            join=join,
            reach=len(reach.get(join, ())),
            is_hub=is_hub,
            caller_must_supply=missing,
            score=score,
        )
        # One workflow per TARGET: a provider wants N distinct things their API does,
        # not the same call reached six ways. Ties break on producer name so the choice
        # is deterministic rather than dict-order luck.
        current = best.get(target)
        if current is None or (candidate.score, current.producer) > (
            current.score,
            candidate.producer,
        ):
            best[target] = candidate

    ranked = sorted(best.values(), key=lambda c: (-c.score, c.target, c.producer))
    return ranked[:limit]


def describe(candidate: Candidate) -> str:
    """One line a human can read, naming the gap when there is one."""
    line = (
        f"{candidate.producer} → {candidate.target}  "
        f"(via `{candidate.join}`, {candidate.reach} operations)"
    )
    if candidate.caller_must_supply:
        missing = ", ".join(f"`{m}`" for m in candidate.caller_must_supply)
        line += f" — you must also supply {missing}"
    return line


def render_index(surface_id: str, candidates: list[Candidate], files: list[str]) -> str:
    """A markdown index of what was emitted. Deterministic, names only."""
    out = [
        f"# {surface_id} — derived agent workflows",
        "",
        f"{len(candidates)} workflow(s), ranked by how much of the API each one's join "
        "carries. Derived from the surface graph: no model call, and nobody authored "
        "these by hand.",
        "",
        "| # | workflow | join | reach | caller must supply | file |",
        "|---|---|---|---|---|---|",
    ]
    for i, (candidate, path) in enumerate(zip(candidates, files), 1):
        supply = (
            ", ".join(f"`{m}`" for m in candidate.caller_must_supply)
            if candidate.caller_must_supply
            else "—"
        )
        out.append(
            f"| {i} | `{candidate.producer}` → `{candidate.target}` | "
            f"`{candidate.join}` | {candidate.reach} | {supply} | `{path}` |"
        )
    out += [
        "",
        "Each file is an Arazzo 1.0 document. A workflow Gecko could not derive "
        "confidently is not written at all — see the refusal notice inside any document "
        "that carries one, and `docs/specs/2026-08-08-composing-with-arazzo.md` for why "
        "a refused plan deliberately fails Arazzo validation.",
        "",
    ]
    return "\n".join(out)
