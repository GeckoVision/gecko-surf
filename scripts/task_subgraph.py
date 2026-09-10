#!/usr/bin/env python3
"""Cut a task-scoped subgraph out of graphify-out/graph.json, and record HOW.

The founder's directive on 2026-09-10, after the graph found two things neither reading
sessions did: use subgraphs to decide, rather than reading whole modules.

WHY THIS IS A SCRIPT AND NOT A QUERY. A plain-words graphify query returns a
NEIGHBOURHOOD. Asked "which modules build or enumerate the MCP tool list an agent sees" it
returned 420 nodes and truncated on the token budget; an independent attempt routed through
`AgentApiClient` at 189 edges and read the same way. A neighbourhood tells you what is
nearby, not what does the work. Seeding from named symbols and expanding a bounded number
of hops gives a mechanism instead.

WHY THE RULE IS WRITTEN INTO THE OUTPUT. A slice whose seed set and traversal are
undocumented is a claim nobody can challenge — the same failure as a metric column nobody
recorded the reading for. Every file this writes carries the seeds, the depth, the
direction and the counts, so a reader can regenerate it or argue with it.

    uv run python scripts/task_subgraph.py 0a-harness \
        --seed retrieval_arms_eval evaluate retrieval_eval --depth 2
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = ROOT / "graphify-out" / "graph.json"


def cut(graph: dict, seeds: list[str], depth: int, direction: str) -> dict:
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    links = graph.get("links") or graph.get("edges") or []

    out_adj: dict[str, set[str]] = {}
    in_adj: dict[str, set[str]] = {}
    for e in links:
        a, b = e.get("source"), e.get("target")
        if a is None or b is None:
            continue
        out_adj.setdefault(a, set()).add(b)
        in_adj.setdefault(b, set()).add(a)

    # A seed matches on id OR label prefix, so a caller can name a module the way a human
    # says it ("evaluate") without knowing the graph's id scheme.
    matched = {
        nid
        for nid, n in nodes.items()
        if any(
            nid == s or str(n.get("label", "")).startswith(s) or nid.startswith(s)
            for s in seeds
        )
    }
    if not matched:
        raise SystemExit(f"no node matched seeds {seeds}")

    keep, frontier = set(matched), deque((n, 0) for n in matched)
    while frontier:
        nid, d = frontier.popleft()
        if d >= depth:
            continue
        nxt: set[str] = set()
        if direction in ("out", "both"):
            nxt |= out_adj.get(nid, set())
        if direction in ("in", "both"):
            nxt |= in_adj.get(nid, set())
        for m in nxt - keep:
            keep.add(m)
            frontier.append((m, d + 1))

    # BOUNDARY EDGES ARE KEPT AND MARKED, and this is not a detail.
    #
    # The obvious slice keeps only edges whose BOTH endpoints survive, which silently drops
    # every edge leaving the set. A module that looks self-contained in such a slice may be
    # the most coupled thing in the repo, and the slice cannot tell you — by construction.
    # (Found by the session that cut the first ingestion subgraph, which had this defect and
    # said so.) An edge with exactly one endpoint inside is kept, flagged `boundary`, and its
    # outside endpoint carried as a stub. Coupling stays visible; the slice stays small.
    inside, boundary = [], []
    outside_ids: set[str] = set()
    for e in links:
        a, b = e.get("source"), e.get("target")
        a_in, b_in = a in keep, b in keep
        if a_in and b_in:
            inside.append(e)
        elif a_in or b_in:
            boundary.append({**e, "boundary": True})
            outside_ids.add(b if a_in else a)

    def _mod(nid: str) -> str:
        n = nodes.get(nid) or {}
        return Path(str(n.get("source_file") or nid)).stem or nid

    # AGGREGATE BEFORE YOU TRAVERSE. Node-level paths route through hubs and read as a
    # neighbourhood; module-to-module counts survive the hub problem because a hub's edges
    # get bucketed by where they land instead of followed. 652 node edges collapsing to 7
    # readable lines is what produced "tools, catalog, surfacedoc and evaluate all read
    # ingest directly" — a mechanism, from data a plain query returned 420 nodes for.
    cross: dict[str, dict[str, int]] = {}
    for e in inside + boundary:
        a, b = _mod(str(e.get("source"))), _mod(str(e.get("target")))
        if a != b:
            cross.setdefault(a, {}).setdefault(b, 0)
            cross[a][b] += 1

    return {
        # The rule, carried with the slice so it can be regenerated or argued with.
        "_selection": {
            "seeds": sorted(seeds),
            "seeds_matched": sorted(matched),
            "depth": depth,
            "direction": direction,
            "source_graph": str(GRAPH.relative_to(ROOT)),
            "nodes_in_source": len(nodes),
            "boundary_edges_kept": True,
        },
        "module_edges": {
            a: dict(sorted(v.items(), key=lambda kv: -kv[1]))
            for a, v in sorted(cross.items())
        },
        "nodes": [nodes[i] for i in sorted(keep)],
        "boundary_nodes": [nodes[i] for i in sorted(outside_ids) if i in nodes],
        "links": inside + boundary,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="task name; output is graphify-out/task-<name>.json")
    ap.add_argument("--seed", nargs="+", required=True)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument(
        "--direction",
        choices=("in", "out", "both"),
        default="both",
        help="in = what reaches the seeds (who reads this), out = what the seeds reach",
    )
    args = ap.parse_args()

    graph = json.loads(GRAPH.read_text(encoding="utf-8"))
    sub = cut(graph, args.seed, args.depth, args.direction)
    dest = ROOT / "graphify-out" / f"task-{args.name}.json"
    dest.write_text(json.dumps(sub, indent=1) + "\n", encoding="utf-8")

    sel = sub["_selection"]
    print(f"wrote {dest.relative_to(ROOT)}")
    print(f"  seeds matched : {len(sel['seeds_matched'])}")
    nb = sum(1 for e in sub["links"] if e.get("boundary"))
    print(
        f"  depth {sel['depth']} {sel['direction']:5} -> {len(sub['nodes'])} nodes, "
        f"{len(sub['links'])} links ({nb} boundary) of {sel['nodes_in_source']}"
    )
    print("\n  module -> module (aggregate first; this is the mechanism):")
    rows = [(a, b, n) for a, v in sub["module_edges"].items() for b, n in v.items()]
    for a, b, n in sorted(rows, key=lambda r: -r[2])[:12]:
        print(f"    {a:26} -> {b:26} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
