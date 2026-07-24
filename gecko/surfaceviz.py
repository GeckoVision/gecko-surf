"""Render the Agent Surface as an SVG call graph — "graphviz for APIs".

The Surface's most persuasive form is a *picture*: operations as nodes, the ``feeds`` edges
as arrows (this call's output flows into that call's input), each arrow colored by its
provenance so the trust ladder is visible at a glance. A dev who sees a messy API become a
clean, provenance-colored graph *feels* the product — this module makes that image.

Deterministic and self-contained, matching the thesis: same graph in → byte-identical SVG
out (everything is sorted); pure stdlib, no graphviz binary, no external layout engine; and
control-plane clean — it draws structure (op ids, join-key names, provenance), never a
payload, value, or secret.

What is drawn: the **plan-eligible** call graph — high-confidence ``feeds`` edges collapsed
to operation→operation, deduplicated per pair keeping the strongest provenance
(``DECLARED`` > ``INFERRED``). That is the graph a plan would actually traverse, not the
full candidate set (which the planner filters). Edge count is capped for legibility and any
drop is reported in the caption — no silent truncation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from html import escape

from .graph import SurfaceGraph

# provenance → (stroke color, dashed) — the trust ladder made visible.
_PROV_STYLE: dict[str, tuple[str, bool]] = {
    "DECLARED": ("#10b981", False),  # emerald — provider-vouched, top of the ladder
    "INFERRED": ("#f59e0b", False),  # amber — derived, high confidence
}
_INFERRED_LOW = (
    "#f59e0b",
    True,
)  # amber dashed — derived, low confidence (rarely drawn)
_PROV_RANK = {"DECLARED": 0, "INFERRED": 1}

#: Cap on drawn edges; excess is reported in the caption, never dropped silently.
_MAX_EDGES = 120
#: Cap on layout columns — bounds width so a dense/cyclic graph can't blow the canvas.
_MAX_LAYERS = 6

# layout constants (px)
_COL_W = 300
_ROW_H = 64
_NODE_W = 230
_NODE_H = 40
_PAD = 40


@dataclass(frozen=True)
class _OpEdge:
    src: str
    dst: str
    provenance: str
    confidence: str | None
    key: str  # the join key (the field/param name that feeds)


def _op_edges(graph: SurfaceGraph, *, high_only: bool = True) -> list[_OpEdge]:
    """Collapse ``feeds`` edges to operation→operation, deduped per pair by best provenance.

    ``high_only`` keeps the plan-eligible edges (what a plan traverses); the full candidate
    set is intentionally not drawn — it is the over-linking the planner filters."""
    by_id = {n.id: n for n in graph.nodes}
    best: dict[tuple[str, str], _OpEdge] = {}
    for e in graph.edges:
        if e.kind != "feeds":
            continue
        if high_only and e.confidence != "high":
            continue
        src_node, dst_node = by_id.get(e.src), by_id.get(e.dst)
        if src_node is None or dst_node is None:
            continue
        src_op, dst_op = src_node.owner, dst_node.owner
        if not src_op or not dst_op or src_op == dst_op:
            continue
        cand = _OpEdge(src_op, dst_op, e.provenance, e.confidence, dst_node.name)
        key = (src_op, dst_op)
        cur = best.get(key)
        if cur is None or _PROV_RANK.get(e.provenance, 9) < _PROV_RANK.get(
            cur.provenance, 9
        ):
            best[key] = cand
    return sorted(best.values(), key=lambda x: (x.src, x.dst))


def _layers(ops: list[str], edges: list[_OpEdge]) -> dict[str, int]:
    """Topological layers by in-degree peeling (Kahn), capped at :data:`_MAX_LAYERS`.

    Peeling (remove in-degree-0 nodes, decrement successors, repeat) keeps the width
    proportional to the graph's real depth — a hub that feeds everything is 2 columns, not
    141. Longest-path relaxation was the bug: on a dense, near-cyclic call graph it pushed
    nodes rightward through cycles and blew the canvas to tens of thousands of px. Nodes
    trapped in cycles (never reach in-degree 0) land in the last placed column — bounded,
    deterministic, honest (they cluster, which a dense API genuinely is)."""
    succ: dict[str, list[str]] = defaultdict(list)
    indeg: dict[str, int] = {o: 0 for o in ops}
    for e in edges:
        if e.dst in indeg and e.src in indeg and e.src != e.dst:
            succ[e.src].append(e.dst)
            indeg[e.dst] += 1

    layer: dict[str, int] = {}
    remaining = dict(indeg)
    frontier = sorted(o for o in ops if remaining[o] == 0)
    lvl = 0
    while frontier:
        capped = min(lvl, _MAX_LAYERS)
        nxt: set[str] = set()
        for o in frontier:
            layer[o] = capped
        for o in frontier:
            for d in succ[o]:
                if d not in layer:
                    remaining[d] -= 1
                    if remaining[d] <= 0:
                        nxt.add(d)
        frontier = sorted(nxt)
        lvl += 1
    # cycle-trapped nodes (never zeroed) go one column past the last placed, capped.
    fallback = min(lvl, _MAX_LAYERS)
    for o in ops:
        layer.setdefault(o, fallback)
    return layer


def _op_label(graph: SurfaceGraph, op_id: str) -> str:
    """`METHOD /path` for an operation node, else the op id."""
    for n in graph.nodes:
        if n.kind == "operation" and n.name == op_id:
            return n.detail or op_id  # detail is "METHOD /path"
    return op_id


def render_svg(graph: SurfaceGraph, *, title: str = "Agent Surface") -> str:
    """The Surface as an SVG call graph. Deterministic, self-contained, control-plane clean."""
    ops = sorted({n.name for n in graph.nodes if n.kind == "operation"})
    all_edges = _op_edges(graph, high_only=True)
    drawn = all_edges[:_MAX_EDGES]
    dropped = len(all_edges) - len(drawn)

    # keep only ops touched by a drawn edge (plus isolated ops so nothing vanishes silently)
    touched = {e.src for e in drawn} | {e.dst for e in drawn}
    shown_ops = sorted(touched) or ops
    layer = _layers(shown_ops, drawn)

    # position: column by layer, row by stable index within the layer.
    by_layer: dict[int, list[str]] = defaultdict(list)
    for o in shown_ops:
        by_layer[layer[o]].append(o)
    pos: dict[str, tuple[float, float]] = {}
    for lyr in sorted(by_layer):
        for i, o in enumerate(sorted(by_layer[lyr])):
            x = _PAD + lyr * _COL_W
            y = _PAD + 60 + i * _ROW_H
            pos[o] = (x, y)

    width = _PAD * 2 + (max(layer.values(), default=0) + 1) * _COL_W
    height = (
        _PAD * 2 + 60 + max((len(v) for v in by_layer.values()), default=1) * _ROW_H
    )

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-monospace,Menlo,monospace">'
    )
    out.append(
        "<defs>"
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#64748b"/></marker></defs>'
    )
    out.append(f'<rect width="{width}" height="{height}" fill="#0b1020"/>')
    out.append(
        f'<text x="{_PAD}" y="28" fill="#e2e8f0" font-size="18" font-weight="700">'
        f"{escape(title)}</text>"
    )

    # edges first (under nodes)
    for e in drawn:
        if e.src not in pos or e.dst not in pos:
            continue
        x1, y1 = pos[e.src]
        x2, y2 = pos[e.dst]
        sx, sy = x1 + _NODE_W, y1 + _NODE_H / 2
        tx, ty = x2, y2 + _NODE_H / 2
        color, dashed = (
            _INFERRED_LOW
            if e.provenance == "INFERRED" and e.confidence == "low"
            else _PROV_STYLE.get(e.provenance, ("#f59e0b", False))
        )
        mx = (sx + tx) / 2
        dash = ' stroke-dasharray="5,4"' if dashed else ""
        out.append(
            f'<path d="M{sx:.0f},{sy:.0f} C{mx:.0f},{sy:.0f} {mx:.0f},{ty:.0f} '
            f'{tx:.0f},{ty:.0f}" fill="none" stroke="{color}" stroke-width="1.6"'
            f'{dash} opacity="0.75" marker-end="url(#arrow)"/>'
        )

    # nodes
    for o in shown_ops:
        nx, ny = pos[o]
        label = _op_label(graph, o)
        method = label.split(" ")[0] if " " in label else ""
        mcolor = {"GET": "#38bdf8", "POST": "#a78bfa", "PUT": "#fbbf24"}.get(
            method, "#64748b"
        )
        out.append(
            f'<rect x="{nx:.0f}" y="{ny:.0f}" width="{_NODE_W}" height="{_NODE_H}" rx="7" '
            f'fill="#1e293b" stroke="{mcolor}" stroke-width="1.5"/>'
        )
        out.append(
            f'<rect x="{nx:.0f}" y="{ny:.0f}" width="4" height="{_NODE_H}" rx="2" fill="{mcolor}"/>'
        )
        shown = label if len(label) <= 30 else label[:29] + "…"
        out.append(
            f'<text x="{nx + 12:.0f}" y="{ny + _NODE_H / 2 + 4:.0f}" fill="#e2e8f0" '
            f'font-size="11">{escape(shown)}</text>'
        )

    # legend + honest caption
    ly = height - 22
    out.append(
        f'<rect x="{_PAD}" y="{ly - 12}" width="14" height="4" fill="#10b981"/>'
        f'<text x="{_PAD + 20}" y="{ly - 6}" fill="#94a3b8" font-size="11">DECLARED (provider-vouched)</text>'
        f'<rect x="{_PAD + 220}" y="{ly - 12}" width="14" height="4" fill="#f59e0b"/>'
        f'<text x="{_PAD + 240}" y="{ly - 6}" fill="#94a3b8" font-size="11">INFERRED (derived)</text>'
    )
    caption = f"{len(shown_ops)} operations · {len(drawn)} plan-eligible edges" + (
        f" (+{dropped} more, not drawn)" if dropped else ""
    )
    out.append(
        f'<text x="{_PAD}" y="{height - 6}" fill="#64748b" font-size="10">'
        f"{escape(caption)}</text>"
    )
    out.append("</svg>")
    return "\n".join(out)


__all__ = ["render_svg"]
