"""The plannable-edge accessor — one place decides which `feeds` edges may be used.

ROOT CAUSE this closes. `feeds_into(dst, high_only=True)` is where the genericity
demotion is applied, but it answers for ONE destination. Anything that wanted every
chainable hop had no accessor to call, so three modules iterated `graph.edges` and
re-implemented edge selection **without the trust dimension**:

* `workflows.derive_candidates` — 34 of 36 Birdeye candidates rested only on demoted
  edges, i.e. the command marketed to providers exactly the joins the planner refuses.
* `surfacereport` — reported "948 chainable hop(s)" where 34 are plannable. That number
  shipped and was quoted twice.

The deeper shape is this repo's own named root cause, seventh instance: `Edge.confidence`
is a field a caller MAY consult, not a state that changes what a caller CAN do. There is
no representation for "not plannable", so the not-plannable case falls into the type's
zero value — an ordinary edge in an ordinary list — and the zero-effort path yields it.

The fix is an accessor with the safe default, plus the guard below: no module outside
`graph.py` may select `kind == "feeds"` off the raw collection.
"""

from __future__ import annotations

import re
from pathlib import Path

from gecko.client import AgentApiClient
from gecko.surface import Surface

REPO = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
BIRDEYE = REPO / "examples/birdeye_demo/spec/birdeye_openapi.json"


def _graph(spec: Path, surface_id: str):
    return Surface.of(AgentApiClient(str(spec), surface_id=surface_id)).graph


def test_accessor_defaults_to_plannable_only() -> None:
    """`feeds_edges()` with no argument returns what the PLANNER would accept.

    The default must be the safe one. A caller who forgets the keyword gets the
    conservative answer, which is the whole point — the previous design made the unsafe
    answer the one you got for free.
    """
    graph = _graph(BIRDEYE, "birdeye")
    plannable = graph.feeds_edges()
    everything = graph.feeds_edges(high_only=False)
    assert plannable, "birdeye has plannable feeds edges"
    assert len(plannable) < len(everything), "birdeye has demoted edges to exclude"
    assert all(e.confidence == "high" for e in plannable)
    assert all(e.kind == "feeds" for e in everything)


def test_accessor_agrees_with_feeds_into_per_destination() -> None:
    """The collection accessor and the per-destination one must not diverge.

    Two implementations of "which edges count" is how the trust filter got lost the
    first time; this pins them to the same answer.
    """
    graph = _graph(BIRDEYE, "birdeye")
    by_dst: dict[str, int] = {}
    for edge in graph.feeds_edges():
        by_dst[edge.dst] = by_dst.get(edge.dst, 0) + 1
    for dst, count in by_dst.items():
        assert len(graph.feeds_into(dst)) == count


def test_no_module_selects_feeds_off_the_raw_collection() -> None:
    """GUARD. The regression that produced two shipped wrong numbers.

    Selecting `kind == "feeds"` while iterating `.edges` re-implements edge selection and
    silently drops the demotion. `graph.py` itself is exempt — it is where the accessor
    lives. Visualization is exempt by name: showing every edge is its job, and it must
    say so explicitly rather than inherit the plannable default.
    """
    exempt = {"graph.py", "surfaceviz.py"}
    offenders: list[str] = []
    for path in sorted((REPO / "gecko").rglob("*.py")):
        if path.name in exempt:
            continue
        text = path.read_text(encoding="utf-8")
        if not re.search(r"\.edges\b", text):
            continue
        # BOTH comparisons — the first draft of this guard only matched `==` and passed
        # while two real offenders used `!= "feeds": continue`. A guard that misses the
        # bug it was written for is worse than no guard.
        if re.search(r'kind\s*[!=]=\s*["\']feeds["\']', text):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        "these modules select `feeds` edges off the raw collection instead of calling "
        f"graph.feeds_edges(): {offenders}. That path drops the genericity demotion — "
        "it is how '948 chainable hops' (34 plannable) shipped."
    )
