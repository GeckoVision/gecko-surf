"""Surface-graph tests (§8) — anchored to real specs, not hand-hinted.

Four properties the design gates on:
- known chains: both TxLINE chains emerge as discoverable plans;
- determinism: same spec in -> byte-identical serialized graph;
- genericity: a generic name (limit/created/status shape) produces no
  plan-eligible feeds edge (the §10 over-linking failure mode);
- anti-poisoning: a malicious feeds-bait field stays quarantined to INFERRED
  with its basis visible, never silently promoted to a plan as if EXTRACTED.
"""

from __future__ import annotations

from pathlib import Path

from gecko.graph import build_graph, plan
from gecko.ingest import extract_operations, load_spec

FIX = Path(__file__).resolve().parent / "fixtures"
TXLINE = FIX / "txline_openapi.yaml"
GENERIC = FIX / "graph_generic.json"
POISONED = FIX / "graph_poisoned.json"


def _graph(path: Path):
    return build_graph(extract_operations(load_spec(str(path))))


# --- known chains ---------------------------------------------------------------
def test_txline_chain1_fixtures_to_odds_via_fixtureid() -> None:
    """fixtures/snapshot --FixtureId--> odds/updates/{fixtureId}, no hand-hints:
    the agent supplies nothing, the planner sources fixtureId."""
    g = _graph(TXLINE)
    p = plan(g, "getApiOddsUpdatesFixtureid", set())
    assert p is not None
    assert p.steps[-1].operation_id == "getApiOddsUpdatesFixtureid"
    # fixtureId is sourced from a fixtures op's produced FixtureId field, INFERRED.
    supplier = p.steps[0]
    assert "fixtureId" in supplier.supplies
    fed = [e for e in p.explain if e.param == "fixtureId"]
    assert fed and fed[0].provenance == "INFERRED"
    assert fed[0].basis == "entity:fixture" and fed[0].confidence == "high"
    assert fed[0].source_field == "FixtureId"


def test_txline_chain2_scores_to_stat_validation_via_seq() -> None:
    """scores/snapshot --seq--> stat-validation. Agent supplies fixtureId + statKey;
    the planner sources the non-id flow key `seq` by statistical rarity."""
    g = _graph(TXLINE)
    p = plan(g, "getApiScoresStat-validation", {"fixtureId", "statKey", "statKey2"})
    assert p is not None
    assert p.steps[-1].operation_id == "getApiScoresStat-validation"
    seq_edges = [e for e in p.explain if e.param == "seq"]
    assert seq_edges, "seq must be sourced from a scores op"
    e = seq_edges[0]
    assert e.provenance == "INFERRED"
    assert e.basis == "rare-key:seq" and e.confidence == "high"
    # the supplier is a scores endpoint that produces seq
    assert e.source_op.startswith("getApiScores")
    assert e.source_field == "seq"


def test_txline_no_plan_when_input_already_satisfied() -> None:
    """No chain is invented when the required input is already in hand."""
    g = _graph(TXLINE)
    # fixtureId supplied -> odds/updates needs nothing more -> a trivial 1-step plan.
    p = plan(g, "getApiOddsUpdatesFixtureid", {"fixtureId"})
    assert p is not None
    assert len(p.steps) == 1
    assert p.explain == ()


# --- determinism ----------------------------------------------------------------
def test_serialization_is_byte_identical_across_builds() -> None:
    g1 = _graph(TXLINE)
    g2 = _graph(TXLINE)
    assert g1.serialize() == g2.serialize()
    assert g1.content_hash() == g2.content_hash()


def test_all_feeds_edges_are_inferred_never_extracted() -> None:
    """Structural anti-poisoning invariant (§2): a feeds link is a derivation,
    never a spec-stated fact — it is ALWAYS INFERRED."""
    g = _graph(TXLINE)
    feeds = [e for e in g.edges if e.kind == "feeds"]
    assert feeds
    assert all(e.provenance == "INFERRED" for e in feeds)
    # produces/consumes/on are the spec-stated facts -> EXTRACTED.
    extracted = [e for e in g.edges if e.provenance == "EXTRACTED"]
    assert {e.kind for e in extracted} <= {"produces", "consumes", "on"}


# --- genericity / false-link control (§10) --------------------------------------
def test_generic_name_produces_no_plan_eligible_feeds_edge() -> None:
    """A `status`-style field produced across many ops is demoted, not linked into
    plans — the generic-name over-linking failure mode the probe measured."""
    g = _graph(GENERIC)
    status_param = next(
        n.id
        for n in g.nodes
        if n.kind == "param" and n.owner == "filterByStatus" and n.name == "status"
    )
    # no HIGH (plan-eligible) feeds edge for the generic name...
    assert g.feeds_into(status_param, high_only=True) == []
    # ...but it is still visible/auditable as a quarantined LOW edge with its basis.
    low = g.feeds_into(status_param, high_only=False)
    assert low and all(
        e.confidence == "low" and e.basis == "generic:status" for e in low
    )
    # and no plan can be built off the quarantined edge -> honest "no confident plan".
    assert plan(g, "filterByStatus", set()) is None


def test_real_entity_key_still_links_when_generic_names_are_demoted() -> None:
    """The genericity floor must not kill the genuine id chain in the same spec."""
    g = _graph(GENERIC)
    p = plan(g, "getOrderDetail", set())
    assert p is not None
    assert p.steps[0].operation_id == "getOrders"
    assert p.explain[0].param == "orderId"
    assert p.explain[0].basis == "entity:order" and p.explain[0].confidence == "high"


# --- poisoned spec (§8) ---------------------------------------------------------
def test_poisoned_feeds_bait_stays_quarantined_to_inferred() -> None:
    """A malicious endpoint advertising a `widgetId` it does not own creates only
    an INFERRED edge with a visible basis — never an EXTRACTED fact, never silently
    laundered into a plan as spec-stated."""
    g = _graph(POISONED)
    stats_param = next(
        n.id
        for n in g.nodes
        if n.kind == "param" and n.owner == "getWidgetStats" and n.name == "widgetId"
    )
    feeds = g.feeds_into(stats_param, high_only=False)
    # the bait op IS a candidate supplier (lexical inference cannot tell it from the
    # legit source) — that is exactly why the control is PROVENANCE, not confidence.
    src_ops = {g._by_id[e.src].owner for e in feeds}
    assert "trackEvent" in src_ops
    # every one of those edges is INFERRED with a recorded, visible basis.
    assert all(e.provenance == "INFERRED" and e.basis for e in feeds)
    # zero EXTRACTED feeds edges anywhere: a poisoned spec cannot forge a spec-fact.
    assert not any(e.kind == "feeds" and e.provenance == "EXTRACTED" for e in g.edges)


def test_poisoned_plan_explain_declares_inferred_provenance() -> None:
    """Whatever supplier the planner picks, the plan's explain surfaces INFERRED —
    the chain 'says so' (§8.4), so a reviewer can audit or disable it."""
    g = _graph(POISONED)
    p = plan(g, "getWidgetStats", set())
    assert p is not None
    assert p.explain
    assert all(e.provenance == "INFERRED" and e.basis for e in p.explain)
