"""Ranked workflow derivation — choosing WHICH workflows an API can offer an agent.

``export-arazzo`` takes ``--op``: the provider must already know the workflow they want.
:mod:`gecko.workflows` removes that by deriving candidates from the graph and ranking
them. These tests cover the ranking (pure, offline) and pin the ONE open question the
derivation cannot answer by itself — see the last test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko.client import AgentApiClient
from gecko.compose import Workspace, cross_plan
from gecko.surface import Surface
from gecko.workflows import derive_candidates, describe, render_index

FIX = Path(__file__).resolve().parent / "fixtures"
PEGANA = FIX / "pegana_p0_openapi.json"
BIRDEYE = Path(__file__).parents[1] / "examples/birdeye_demo/spec/birdeye_openapi.json"


def _graph(spec: Path, surface_id: str, hints: dict[str, str] | None = None):
    return Surface.of(
        AgentApiClient(str(spec), surface_id=surface_id, declared_hints=hints)
    ).graph


def test_candidates_start_at_an_entry_point_and_end_somewhere_that_needs_input() -> (
    None
):
    """The shape of a candidate: callable-with-nothing feeding needs-something.

    A hop between two entry points is not a workflow (either could be called first), and
    a hop INTO an entry point is not one either (the target never needed the value).
    """
    graph = _graph(BIRDEYE, "birdeye")
    for candidate in derive_candidates(graph, limit=10):
        assert not list(graph.required_inputs(candidate.producer)), (
            f"{candidate.producer} is a producer but needs inputs — not an entry point"
        )
        assert list(graph.required_inputs(candidate.target)), (
            f"{candidate.target} is a target but needs nothing — not a workflow"
        )


def test_ranking_puts_the_spine_first() -> None:
    """A hop through the handle most of the API depends on outranks a niche one.

    Birdeye's spine is `address` (44 of 89 operations). If a candidate through a field
    used twice outranked it, the provider's top workflow would be an afterthought of
    their own API.
    """
    ranked = derive_candidates(_graph(BIRDEYE, "birdeye"), limit=7)
    assert ranked, "birdeye must yield candidates"
    assert ranked[0].reach >= ranked[-1].reach
    assert ranked == sorted(ranked, key=lambda c: (-c.score, c.target, c.producer))


def test_one_workflow_per_target() -> None:
    """A provider wants N distinct things their API does, not one call reached N ways."""
    ranked = derive_candidates(_graph(PEGANA, "pegana"), limit=20)
    targets = [c.target for c in ranked]
    assert len(targets) == len(set(targets))


def test_floor_is_named_not_hidden() -> None:
    """A target that ALSO needs a value nothing here produces is still emitted, ranked
    lower, with the gap stated. Dropping it would hide the most interesting fact about
    the API; emitting it silently would produce a workflow that fails at run time."""
    ranked = derive_candidates(_graph(PEGANA, "pegana"), limit=20)
    gapped = [c for c in ranked if c.caller_must_supply]
    assert gapped, "pegana has targets needing outside values"
    for candidate in gapped:
        assert not candidate.self_contained
        assert all(
            f"`{m}`" in describe(candidate) for m in candidate.caller_must_supply
        )
    # ranked below an equally-reaching self-contained peer
    for gap in gapped:
        peers = [c for c in ranked if c.self_contained and c.reach == gap.reach]
        for peer in peers:
            assert peer.score > gap.score


def test_derivation_is_deterministic() -> None:
    graph = _graph(BIRDEYE, "birdeye")
    assert derive_candidates(graph, limit=7) == derive_candidates(graph, limit=7)


def test_index_names_every_emitted_file() -> None:
    ranked = derive_candidates(_graph(PEGANA, "pegana"), limit=3)
    files = [f"pegana.{c.target}.arazzo.json" for c in ranked]
    index = render_index("pegana", ranked, files)
    for candidate, path in zip(ranked, files):
        assert candidate.target in index
        assert path in index


def test_empty_surface_yields_nothing_rather_than_a_guess() -> None:
    class _Empty:
        nodes: list = []
        edges: list = []

        def required_inputs(self, _name: str) -> list:
            return []

    assert derive_candidates(_Empty(), limit=5) == []  # type: ignore[arg-type]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN, routed to defi-security-engineer — not a bug to patch here. A derived "
        "candidate is not yet a plannable chain: cross_plan inserts the producer step "
        "only when the join is a CONFIRMED entity, so `gecko workflows` currently emits "
        "a refusal for every candidate it found. Confirming the join automatically would "
        "widen a provenance tier, which is a gated change. The distinction to adjudicate: "
        "a CROSS-API join must be a customer act (§13.6 — an untrusted spec must never "
        "mint an executable cross-surface chain), but an INTRA-surface join has the same "
        "provider's document on both sides. Those may not be the same trust problem. "
        "Remove this xfail in the PR that settles it."
    ),
)
def test_a_derived_candidate_is_plannable_without_manual_confirmation() -> None:
    graph = _graph(BIRDEYE, "birdeye")
    candidate = derive_candidates(graph, limit=1)[0]
    plan = cross_plan(Workspace(graphs=(graph,)), "birdeye", candidate.target, set())
    assert plan is not None, "the derivation found this hop; planning must agree"
    assert [s.operation_id for s in plan.steps] == [
        candidate.producer,
        candidate.target,
    ]
