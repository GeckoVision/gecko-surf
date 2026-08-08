"""The surface graph report — what the graph says about an API, offline and $0.

The Scorecard grades. This describes: the spine (what most operations hang off), the
chains (producer -> consumer hops), and the floor (inputs nothing here produces). Each
assertion below is about a property of the REPORT, not about a number that happens to
fall out of one fixture today — a test pinned to "16" breaks on any spec edit and
teaches nobody anything.

Control plane is the load-bearing one: a report is something a provider pastes into a
ticket, so a value leaking into it is a data-governance failure, not a cosmetic bug.
"""

from __future__ import annotations

from pathlib import Path

from gecko.client import AgentApiClient
from gecko.surface import Surface
from gecko.surfacereport import (
    build_report,
    counts,
    render_markdown,
    summarize,
)

FIX = Path(__file__).resolve().parent / "fixtures"
PEGANA = FIX / "pegana_p0_openapi.json"


def _report(surface_id: str = "pegana"):
    surface = Surface.of(AgentApiClient(str(PEGANA), surface_id=surface_id))
    return build_report(surface.graph, surface_id)


def test_spine_ranks_joinable_handles_above_plumbing() -> None:
    """A hub (produced AND consumed here) outranks an input-only name.

    `limit` appears in six operations and `asset` in sixteen, but reach alone is the
    wrong order: `limit` is pagination and can never be chained on, while `asset` is
    what the API is actually about. Ranking by reach would bury the finding under
    plumbing. Input-only names stay visible, demoted rather than hidden.
    """
    spine = _report().spine
    assert spine, "pegana must have a spine"
    hubs = [s for s in spine if s.is_hub]
    inputs_only = [s for s in spine if not s.is_hub]
    assert hubs, "expected at least one joinable handle"
    if inputs_only:
        assert spine.index(hubs[-1]) < spine.index(inputs_only[0]), (
            "every hub must rank above every input-only name"
        )


def test_floor_names_inputs_no_operation_produces() -> None:
    """The floor is the finding a specification structurally cannot give you.

    A spec describes each call in isolation, so an input with no producer looks exactly
    like one that has one. Only the graph can tell them apart.
    """
    report = _report()
    assert report.floor, "pegana has operations needing outside values"
    produced = {c.name for c in report.chains}
    for entry in report.floor:
        assert entry.missing, "a floor entry must name what is missing"
        for name in entry.missing:
            assert name not in produced, (
                f"{name!r} is listed as unproducible yet appears as a chain output"
            )


def test_entry_points_need_nothing() -> None:
    """An entry point is callable with no prior context — every path starts at one."""
    surface = Surface.of(AgentApiClient(str(PEGANA), surface_id="pegana"))
    report = build_report(surface.graph, "pegana")
    for name in report.entry_points:
        assert not list(surface.graph.required_inputs(name)), (
            f"{name!r} is listed as an entry point but has required inputs"
        )


def test_report_is_deterministic() -> None:
    """Same surface in, byte-identical markdown out. No model, no clock, no ordering
    luck — a report that shifts between runs cannot be diffed, and a report that cannot
    be diffed cannot show drift."""
    assert render_markdown(_report()) == render_markdown(_report())


def test_report_carries_no_values() -> None:
    """CONTROL PLANE. The report names operations, handles and provenance — never a
    value, an example, or a response payload. A provider pastes this into a ticket.
    """
    markdown = render_markdown(_report())
    spec_text = PEGANA.read_text()
    import json

    spec = json.loads(spec_text)

    leaked: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("example", "examples", "default", "const"):
                    for candidate in _strings(value):
                        # Long, distinctive literals only: a value like "USD" is also an
                        # ordinary English token and would false-positive forever.
                        if len(candidate) >= 12 and candidate in markdown:
                            leaked.append(candidate)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(spec)
    assert not leaked, f"example values leaked into the report: {leaked[:3]}"


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    if isinstance(node, dict):
        return [s for item in node.values() for s in _strings(item)]
    return []


def test_summary_is_one_honest_line() -> None:
    """The terminal a-ha, before anyone opens the file. It must state the floor rather
    than only the good news — a summary that hides what the API cannot do is a pitch."""
    report = _report()
    line = summarize(report)
    assert "operations" in line
    assert str(len(report.floor)) in line
    assert "\n" not in line


def test_empty_graph_says_so_rather_than_rendering_a_confident_blank() -> None:
    """A surface with nothing to report must SAY there is nothing, not print empty
    tables. This is the not-evaluated class: a blank section reads as 'no problems'."""

    class _Empty:
        nodes: list = []
        edges: list = []

        def required_inputs(self, _name: str) -> list:
            return []

    report = build_report(_Empty(), "empty")  # type: ignore[arg-type]
    markdown = render_markdown(report)
    assert counts(report)["spine"] == 0
    assert "no shared handle" in markdown
    assert "Every call" in markdown or "every call" in markdown


def test_summary_reports_the_count_not_the_display_cap() -> None:
    """REGRESSION. `summarize` reported len(chains), which is truncated to _TOP_N — so a
    surface with 948 chainable hops printed "8 chainable hop(s)". The display cap read as
    a measurement, and that number was quoted before anyone noticed.

    A number that can be quoted must never be a truncation. `chains` is what is SHOWN;
    `total_chains` is what was FOUND, and only the latter may appear in a summary.
    """
    report = _report()
    assert report.total_chains >= len(report.chains)
    assert f"{report.total_chains} chainable" in summarize(report)
    if report.total_chains > len(report.chains):
        markdown = render_markdown(report)
        assert f"Showing {len(report.chains)} of {report.total_chains}" in markdown
