"""The graph report for an API surface — what the graph says about the API.

The Scorecard answers *"is this API agent-ready?"* with a grade. Useful, and a
judgement. This answers a different question, the one a code knowledge graph answers
about a codebase: **what IS this thing, structurally, and what will it not tell you?**

Three sections, each derived from the graph and nothing else:

* **The spine** — the entities the most operations hang off. An API's real subject is
  not in its title; it is whatever every other call needs a handle on. Pegana's is
  ``asset`` (16 of 43 operations), Birdeye's is ``address`` (44 of 89). Nobody documents
  this, because from the inside it is too obvious to write down.
* **The chains** — ``feeds`` edges: a field one operation returns that another accepts.
  This is what lets an agent go from "I know a symbol" to "I can call the thing that
  needs a mint" without a human drawing the arrow. Each carries its provenance.
* **The floor** — operations whose required inputs NOTHING in this surface produces.
  You must bring those values yourself, and a spec will never say so: it describes each
  call in isolation, so an input that has no producer looks exactly like one that does.

Control plane throughout: the report names operations, parameters and entities. It never
contains a value, an example, or a response payload — the same rule the corpus follows.
Deterministic: same surface in, byte-identical report out, no model call anywhere.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .graph import Node, SurfaceGraph

#: Entities touched by fewer than this many operations are noise, not a spine. Two is
#: the floor for "recurring" (one operation touching a name says nothing structural).
_SPINE_MIN_OPS = 2

#: How many rows each section prints. A report nobody finishes is a report nobody reads;
#: the full data is always in the graph for anyone who wants it.
_TOP_N = 8


@dataclass(frozen=True)
class SpineEntry:
    """A recurring handle and how much of the surface depends on it."""

    name: str
    operations: int
    as_input: int
    as_output: int

    @property
    def is_hub(self) -> bool:
        """True when the name is both produced and consumed here — a real join, not
        just a repeated parameter. These are the ones an agent can actually chain on."""
        return self.as_input > 0 and self.as_output > 0


@dataclass(frozen=True)
class ChainEntry:
    """One producer→consumer hop the surface can satisfy on its own."""

    producer: str
    consumer: str
    name: str
    provenance: str


@dataclass(frozen=True)
class FloorEntry:
    """An operation with a required input no operation in this surface produces."""

    operation: str
    missing: tuple[str, ...]


@dataclass(frozen=True)
class SurfaceReport:
    """The three findings, as data. Rendering is separate so a caller can consume this
    without parsing markdown (the Playground and the Scorecard both may later)."""

    surface_id: str
    operations: int
    spine: tuple[SpineEntry, ...] = ()
    chains: tuple[ChainEntry, ...] = ()
    floor: tuple[FloorEntry, ...] = ()
    #: Names of operations that need nothing at all — the honest entry points. An agent
    #: with no context can call exactly these, and every other path starts at one.
    entry_points: tuple[str, ...] = field(default=())


def _short(node_id: str) -> str:
    """The readable tail of a namespaced node id (``surface::param:op:X:in:y`` -> ``X``)."""
    if "op:" not in node_id:
        return node_id.rsplit(":", 1)[-1]
    tail = node_id.split("op:", 1)[1]
    return tail.split(":", 1)[0].split("::", 1)[0]


def build_report(graph: SurfaceGraph, surface_id: str = "") -> SurfaceReport:
    """Derive the report from the graph. Pure: no I/O, no network, no model."""
    ops = [n for n in graph.nodes if n.kind == "operation"]
    op_names = {n.name for n in ops}

    produced: set[str] = set()
    consumed: set[str] = set()
    per_name_ops: dict[str, set[str]] = defaultdict(set)
    inputs_of: dict[str, set[str]] = defaultdict(set)

    for node in graph.nodes:
        if node.kind not in ("param", "field"):
            continue
        owner = node.owner if node.owner in op_names else _short(node.id)
        per_name_ops[node.name].add(owner)
        if node.kind == "field":
            produced.add(node.name)
        else:
            consumed.add(node.name)
            inputs_of[owner].add(node.name)

    spine = tuple(
        sorted(
            (
                SpineEntry(
                    name=name,
                    operations=len(owners),
                    as_input=1 if name in consumed else 0,
                    as_output=1 if name in produced else 0,
                )
                for name, owners in per_name_ops.items()
                if len(owners) >= _SPINE_MIN_OPS
            ),
            # Hubs first, then by reach. A name that is both produced and consumed here
            # is something an agent can CHAIN on; a name that is only ever an input is
            # pagination or a flag. `limit` in six operations says nothing structural,
            # and ranking it beside `asset` would bury the finding under plumbing.
            # Input-only names still appear — demoted, not hidden.
            key=lambda s: (not s.is_hub, -s.operations, s.name),
        )[:_TOP_N]
    )

    seen: set[tuple[str, str, str]] = set()
    chains: list[ChainEntry] = []
    for edge in graph.edges:
        if edge.kind != "feeds":
            continue
        producer, consumer = _short(edge.src), _short(edge.dst)
        name = edge.dst.rsplit(":", 1)[-1]
        key = (producer, consumer, name)
        if producer == consumer or key in seen:
            continue
        seen.add(key)
        chains.append(
            ChainEntry(
                producer=producer,
                consumer=consumer,
                name=name,
                provenance=str(getattr(edge, "provenance", "") or "extracted"),
            )
        )
    chains.sort(key=lambda c: (c.producer, c.consumer, c.name))

    floor: list[FloorEntry] = []
    entry_points: list[str] = []
    for op in ops:
        required = [n.name for n in _required(graph, op)]
        if not required:
            entry_points.append(op.name)
            continue
        missing = tuple(sorted(n for n in required if n not in produced))
        if missing:
            floor.append(FloorEntry(operation=op.name, missing=missing))
    floor.sort(key=lambda f: f.operation)

    return SurfaceReport(
        surface_id=surface_id or getattr(graph, "surface_id", "") or "surface",
        operations=len(ops),
        spine=spine,
        chains=tuple(chains[:_TOP_N]),
        floor=tuple(floor),
        entry_points=tuple(sorted(entry_points)),
    )


def _required(graph: SurfaceGraph, op: Node) -> list[Node]:
    """Required inputs for an operation, tolerating a graph that cannot answer."""
    try:
        return list(graph.required_inputs(op.name))
    except Exception:
        return []


def render_markdown(report: SurfaceReport) -> str:
    """The report as markdown. Deterministic — same input, byte-identical output."""
    out: list[str] = [
        f"# {report.surface_id} — surface graph report",
        "",
        f"{report.operations} operations. Derived from the graph alone: no model call, "
        "no live request, no value from any response.",
        "",
        "## The spine",
        "",
    ]
    if report.spine:
        out += [
            "What most of this API hangs off. An API's real subject is rarely in its "
            "title — it is whatever every other call needs a handle on.",
            "",
            "| handle | operations | joinable |",
            "|---|---|---|",
        ]
        out += [
            f"| `{s.name}` | {s.operations} | {'yes — produced and consumed here' if s.is_hub else 'input only'} |"
            for s in report.spine
        ]
    else:
        out.append(
            "No name recurs across two or more operations — this surface has no "
            "shared handle, so every call stands alone."
        )
    out += ["", "## The chains", ""]
    if report.chains:
        out += [
            "A field one operation returns that another accepts — the hops an agent can "
            "make without you drawing the arrow.",
            "",
            "| from | to | via | provenance |",
            "|---|---|---|---|",
        ]
        out += [
            f"| `{c.producer}` | `{c.consumer}` | `{c.name}` | {c.provenance} |"
            for c in report.chains
        ]
    else:
        out.append(
            "No operation here produces a value another one accepts. Every call "
            "needs its inputs from outside."
        )
    out += ["", "## The floor", ""]
    if report.floor:
        out += [
            "Operations with a required input **nothing in this surface produces**. You "
            "must supply these yourself. A specification cannot tell you this: it "
            "describes each call in isolation, so an input with no producer looks "
            "exactly like one that has one.",
            "",
            "| operation | you must supply |",
            "|---|---|",
        ]
        out += [
            f"| `{f.operation}` | {', '.join(f'`{m}`' for m in f.missing)} |"
            for f in report.floor
        ]
    else:
        out.append(
            "Every required input is produced somewhere in this surface. An agent "
            "starting from an entry point can reach every operation."
        )
    if report.entry_points:
        out += [
            "",
            "## Entry points",
            "",
            "Callable with no prior context — every path starts at one of these.",
            "",
        ] + [f"- `{name}`" for name in report.entry_points[:_TOP_N]]
    out.append("")
    return "\n".join(out)


def summarize(report: SurfaceReport) -> str:
    """One line for a terminal — the a-ha, before anyone opens the file."""
    top = report.spine[0] if report.spine else None
    spine = (
        f"{report.operations} operations hang off `{top.name}` ({top.operations} of them)"
        if top
        else f"{report.operations} operations, no shared handle"
    )
    return (
        f"{spine} · {len(report.chains)} chainable hop(s) · "
        f"{len(report.floor)} operation(s) need a value this API never returns"
    )


def counts(report: SurfaceReport) -> Counter[str]:
    """Section sizes — for the Scorecard and tests, never for a claim."""
    return Counter(
        spine=len(report.spine),
        chains=len(report.chains),
        floor=len(report.floor),
        entry_points=len(report.entry_points),
    )
