"""Pegana correlation surface, Phase 1 — the cross-API mint join, offline ($0, Pattern B).

The delivery: "one query -> many correlated services," joined by the Solana **token
mint** value-domain (``solana-token-mint``, ``gecko/vindex.py``). Pegana (the peg-risk
anchor), Birdeye (price/liquidity/security), and Jupiter (exit route) all key off the
same on-chain mint — but each spec NAMES that param differently (Pegana ``mint``,
Birdeye ``address`` *and* ``token_address``, Jupiter ``inputMint`` / ``outputMint``),
and none ship an ``x-gecko`` entity. So neither name equality nor a schema signature
can carry the join (the §13.6 finding, reproduced here on three real specs): the mint
correlation exists ONLY as a **DECLARED** value-domain join, **customer-CONFIRMED**
before it is plan-eligible.

Everything here runs off the committed fixtures and never touches the network. The
load-bearing asserts:

1. **DECLARED-found across the three surfaces** — with the mint value-domain declared,
   ``correlate_surfaces`` / ``value_domain_index`` join Pegana <-> Birdeye <-> Jupiter
   by entity (not by name), each edge carrying its provenance basis.
2. **Confirm -> plan-eligible** — a customer-confirmed join lets ``cross_plan`` recover
   an executable Pegana -> Birdeye chain (first-plan-correct).
3. **The gate holds both ways** — BEFORE confirm the same chain is refused; and a
   provider-only DECLARED edge (untrusted ``x-gecko``, unconfirmed) is *found* but stays
   a quarantined candidate that can NOT mint an executable chain (the shipped
   ``graph.confirmed`` invariant — asserted, never weakened).

Falsified if a mint join reaches plan-eligibility without a customer-confirmed DECLARED
entity, or if the pristine specs correlate on name/signature alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gecko.compose import Workspace, cross_plan
from gecko.correlate import correlate_surfaces
from gecko.hints import confirm_entity, declared_entity_hints, load_confirmed
from gecko.surface import Surface
from gecko.vindex import value_domain_index

_FIXTURES = Path(__file__).parent / "fixtures"
_PEGANA = _FIXTURES / "pegana_p0_openapi.json"
_BIRDEYE = Path(__file__).parents[1] / "examples/birdeye_demo/spec/birdeye_openapi.json"
_JUPITER = Path(__file__).parents[1] / "gecko/examples/jupiter_swap_openapi.json"

#: the value-domain the customer declares across the three surfaces. ``vindex.py`` owns
#: this vocabulary; the entity normalizes (hyphens stripped, lowercased) to the key below.
_VD = "solana-token-mint"
_ENTITY = "solanatokenmint"

#: The mint param/field names each surface uses — differ across (and within) the specs,
#: which is exactly why only a DECLARED value-domain join can bridge them.
_PEGANA_MINT = {"mint": _VD}
_BIRDEYE_MINT = {"address": _VD, "token_address": _VD}
_JUPITER_MINT = {"inputMint": _VD, "outputMint": _VD}

#: Birdeye's ``get-defi-multi_price`` consumes ``list_address`` — a mint input Birdeye
#: never EMITS as a response field, so once declared it can only be sourced cross-surface.
#: That asymmetry is what forces a genuine Pegana -> Birdeye chain out of ``cross_plan``
#: (which is intra-first: any input a surface can self-supply never crosses a boundary).
_BIRDEYE_XLIST = {"list_address": _VD}
_MULTI_PRICE = "get-defi-multi_price"


def _spec(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _surface(path: Path, sid: str, hints: dict[str, str] | None = None) -> Surface:
    # ``declared_hints`` is the customer-CONFIRMED vocabulary (client wires it into
    # graph.confirmed) — the offline equivalent of ``gecko graph confirm``.
    return Surface.from_spec(
        _spec(path), base_url="https://x", surface_id=sid, declared_hints=hints or {}
    )


def _inject_provider_marker(spec: dict[str, Any], param_name: str) -> dict[str, Any]:
    """Overlay a provider ``x-gecko-entity`` marker on every param object named
    ``param_name`` (untrusted spec-authored declaration). In-memory only — the committed
    fixtures stay pristine; this stages the "provider-declared, unconfirmed" tier."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("name") == param_name and "schema" in node:
                node["x-gecko-entity"] = _VD
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    return spec


# --- the specs ship no mint declaration (the finding these DECLARED joins answer) ------
def test_specs_ship_no_mint_entity_and_ingest_clean() -> None:
    """None of the three real specs declare the mint value-domain (no ``x-gecko``), and
    each ingests clean (Skill Guard quarantines nothing). The join is Gecko/customer
    work, not something a provider handed us — the honest starting point."""
    for path in (_PEGANA, _BIRDEYE, _JUPITER):
        assert declared_entity_hints(_spec(path)) == {}, (
            f"{path.name} unexpectedly declares an entity"
        )

    for path, sid in (
        (_PEGANA, "pegana"),
        (_BIRDEYE, "birdeye"),
        (_JUPITER, "jupiter"),
    ):
        assert _surface(path, sid).safety.clean, (
            f"{sid} unexpectedly quarantined a tool"
        )


# --- PROOF 1: DECLARED-found across the three surfaces, by value-domain not by name -----
def test_mint_join_declared_found_across_three_surfaces() -> None:
    pegana = _surface(_PEGANA, "pegana", _PEGANA_MINT)
    birdeye = _surface(_BIRDEYE, "birdeye", _BIRDEYE_MINT)
    jupiter = _surface(_JUPITER, "jupiter", _JUPITER_MINT)

    # Pegana `mint` (produced) <-> Birdeye `address` (consumed): a DECLARED tier-3 edge
    # across the API boundary, carrying its provenance basis — the names DIFFER, only the
    # value-domain entity bridges them.
    links = correlate_surfaces(pegana, birdeye).links
    mint_to_addr = [
        lk
        for lk in links
        if lk.basis.src_field == "mint"
        and lk.basis.dst_param == "address"
        and lk.basis.src_surface == "pegana"
        and lk.basis.dst_surface == "birdeye"
    ]
    assert mint_to_addr, "expected a DECLARED Pegana mint -> Birdeye address cross link"
    edge = mint_to_addr[0]
    assert edge.basis.tier == 3
    assert edge.basis.provenance == "DECLARED"
    assert edge.basis.entity == _ENTITY
    assert edge.plan_eligible  # both sides customer-confirmed
    assert f"declared:{_ENTITY}" in edge.basis.signals

    # value_domain_index: one bucket, all three providers under the mint entity.
    index = value_domain_index([pegana, birdeye, jupiter])
    assert index.entities() == (_ENTITY,)
    group = index.group(_ENTITY)
    assert group is not None
    assert {ref.surface_id for ref in group.producers} == {
        "pegana",
        "birdeye",
        "jupiter",
    }
    assert {ref.surface_id for ref in group.consumers} == {
        "pegana",
        "birdeye",
        "jupiter",
    }

    # find_correlations: the cross-provider joins span every surface pair, each carrying
    # both sides' confirmed provenance — this IS "one value-domain -> many services."
    joins = index.find_correlations(_ENTITY)
    cross = [j for j in joins if j.producer.surface_id != j.consumer.surface_id]
    assert cross, "expected cross-provider mint joins"
    assert all(j.plan_eligible for j in cross)
    assert all(j.producer.confirmed and j.consumer.confirmed for j in cross)
    pairs = {(j.producer.surface_id, j.consumer.surface_id) for j in cross}
    assert {"pegana", "birdeye", "jupiter"} <= {s for pair in pairs for s in pair}


# --- PROOF 2: confirm -> plan-eligible; cross_plan recovers a Pegana -> Birdeye chain ---
def test_confirmed_join_makes_cross_plan_first_plan_correct() -> None:
    pegana = _surface(_PEGANA, "pegana", _PEGANA_MINT)
    birdeye = _surface(_BIRDEYE, "birdeye", _BIRDEYE_XLIST)
    workspace = Workspace(graphs=(pegana.graph, birdeye.graph))

    plan = cross_plan(workspace, "birdeye", _MULTI_PRICE, set())
    assert plan is not None, "confirmed mint join should recover a cross-surface chain"

    # the chain ends at the Birdeye price-by-mint op, sourced from Pegana (Pegana first).
    assert plan.steps[-1].surface == "birdeye"
    assert plan.steps[-1].operation_id == _MULTI_PRICE
    assert any(step.surface == "pegana" for step in plan.steps)

    # the cross edge is DECLARED, entity-basis, sourced from Pegana — auditable end to end.
    cross_edges = [e for e in plan.explain if e.source_surface == "pegana"]
    assert cross_edges, "expected a DECLARED cross edge sourced from Pegana"
    assert all(e.provenance == "DECLARED" for e in cross_edges)
    assert all(e.basis == f"declared:{_ENTITY}" for e in cross_edges)
    assert any(e.param == "list_address" for e in cross_edges)


# --- PROOF 3a: BEFORE confirm, the same chain is refused (fail-closed) ------------------
def test_unconfirmed_join_is_refused() -> None:
    """No customer confirmation -> the mint entity is absent from every surface's vocab,
    so the differently-named params never join and ``cross_plan`` refuses (honest None)."""
    pegana = _surface(_PEGANA, "pegana")  # pristine, no declared_hints
    birdeye = _surface(_BIRDEYE, "birdeye")
    workspace = Workspace(graphs=(pegana.graph, birdeye.graph))

    assert cross_plan(workspace, "birdeye", _MULTI_PRICE, set()) is None

    # and no cross-API tier-3 link exists at all (name/signature can't bridge the specs).
    cross = [
        lk
        for lk in correlate_surfaces(pegana, birdeye).links
        if lk.basis.src_surface != lk.basis.dst_surface and lk.basis.tier == 3
    ]
    assert cross == []


# --- PROOF 3b: a provider-only DECLARED edge is FOUND but can't mint a chain ------------
def test_provider_declared_unconfirmed_is_candidate_not_planned() -> None:
    """The shipped ``graph.confirmed`` invariant on the real surfaces: an untrusted
    provider ``x-gecko`` declaration (unconfirmed) surfaces the join as a DECLARED tier-3
    *candidate* — visible, provenance-tagged — but NOT plan-eligible, and ``cross_plan``
    refuses it. An untrusted spec cannot unilaterally mint a cross-system executable
    join. Asserted, not weakened (any change to this gate routes to defi-security)."""
    pegana = Surface.from_spec(
        _inject_provider_marker(_spec(_PEGANA), "mint"),
        base_url="https://x",
        surface_id="pegana",
    )
    birdeye = Surface.from_spec(
        _inject_provider_marker(_spec(_BIRDEYE), "list_address"),
        base_url="https://x",
        surface_id="birdeye",
    )

    # the join is FOUND — provider-declared, tier 3, DECLARED — but quarantined.
    candidates = [
        lk
        for lk in correlate_surfaces(pegana, birdeye).links
        if lk.basis.dst_param == "list_address"
        and lk.basis.src_surface == "pegana"
        and lk.basis.dst_surface == "birdeye"
        and lk.basis.tier == 3
    ]
    assert candidates, "provider-declared mint join should be found as a candidate"
    candidate = candidates[0]
    assert candidate.basis.provenance == "DECLARED"
    assert not candidate.plan_eligible and candidate.candidate
    assert "provider-declared-unconfirmed" in candidate.basis.signals

    # the gate holds all the way to the executable plan: cross_plan refuses it.
    workspace = Workspace(graphs=(pegana.graph, birdeye.graph))
    assert cross_plan(workspace, "birdeye", _MULTI_PRICE, set()) is None


# --- the literal `gecko graph confirm` path: persist -> load -> plan-eligible -----------
def test_graph_confirm_roundtrip_reaches_plan_eligible(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Exercise the real confirm store (``gecko graph confirm``): ``confirm_entity``
    persists the customer vouch, ``load_confirmed`` reads it back, and feeding that into
    the surfaces yields the same plan-eligible cross chain. Offline — a tmp config home,
    never the founder's ``~/.gecko``."""
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))

    confirm_entity("pegana", "mint", _VD, prior_basis="value-domain: solana-token-mint")
    confirm_entity("birdeye", "list_address", _VD)

    pegana = _surface(_PEGANA, "pegana", load_confirmed("pegana"))
    birdeye = _surface(_BIRDEYE, "birdeye", load_confirmed("birdeye"))
    workspace = Workspace(graphs=(pegana.graph, birdeye.graph))

    plan = cross_plan(workspace, "birdeye", _MULTI_PRICE, set())
    assert plan is not None
    assert plan.steps[-1].operation_id == _MULTI_PRICE
    assert any(step.surface == "pegana" for step in plan.steps)
