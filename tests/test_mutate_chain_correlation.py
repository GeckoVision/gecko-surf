"""The cross-API MUTATE chain that ingestion slice 2 makes correlate (§13.5).

API A (pool oracle) EMITS a ``poolAddress`` in a response; API B (position manager)
CONSUMES it in a POST **body** — a create-referencing-Y's-id mutate. Slice 2 decomposes
the request-body field into a typed correlation TARGET and lifts the base58 example (via
the OpenAPI ``examples`` list) onto BOTH the producer response field and the consumer body
field, so the value-domain signature corroborates the join (``format-eq``).

The DECLARED+CONFIRMED gate is asserted BOTH WAYS (§13.6): a provider-declared-only entity
is a quarantined CANDIDATE (never plan-eligible); only a CUSTOMER confirm makes the
cross-API mutate join plan-eligible. Enriching the producer/consumer surface does NOT
loosen that gate.
"""

from __future__ import annotations

from pathlib import Path

from gecko.access import public_session
from gecko.correlate import correlate_surfaces
from gecko.surface import Surface
from gecko.vindex import value_domain_index

_FIX = Path(__file__).parent / "fixtures"
_ORACLE = _FIX / "mutate_pool_oracle_openapi.json"
_MGR = _FIX / "mutate_position_mgr_openapi.json"
_POOL_ENTITY = "solana-pool"
_POOL_NORM = "solanapool"


def _oracle(*, confirm: bool) -> Surface:
    hints = {"poolAddress": _POOL_ENTITY} if confirm else {}
    return Surface.from_spec(
        str(_ORACLE),
        session=public_session(),
        surface_id="oracle",
        declared_hints=hints,
    )


def _mgr(*, confirm: bool) -> Surface:
    hints = {"poolAddress": _POOL_ENTITY} if confirm else {}
    return Surface.from_spec(
        str(_MGR), session=public_session(), surface_id="mgr", declared_hints=hints
    )


def _mutate_link(res):
    """The getPool(response) -> openPosition(body) mutate link, or None."""
    for lk in res.links:
        if lk.src_op == "getPool" and lk.dst_op == "openPosition":
            return lk
    return None


# --- the response-field -> body-field mutate correlation is FOUND ----------------
def test_response_field_feeds_body_field_is_found_as_declared() -> None:
    """The producer response field poolAddress correlates to the consumer BODY field
    poolAddress — the mutate side that was not a typed target before slice 2."""
    res = correlate_surfaces(_oracle(confirm=False), _mgr(confirm=False))
    link = _mutate_link(res)
    assert link is not None, "the response->body mutate correlation was not found"
    assert link.basis.src_field == "poolAddress"  # the response producer field
    assert link.basis.dst_param == "poolAddress"  # the request-BODY consumer field
    assert link.basis.provenance == "DECLARED"
    assert link.basis.tier == 3
    assert link.basis.entity == _POOL_NORM


def test_the_base58_example_corroborates_the_join() -> None:
    """The enrichment: the base58 example (declared via the ``examples`` list) reaches the
    value-domain signature on BOTH sides, so the join carries a ``format-eq`` corroborator
    — proof the producer/consumer surface got richer, not just wider."""
    res = correlate_surfaces(_oracle(confirm=True), _mgr(confirm=True))
    link = _mutate_link(res)
    assert link is not None
    assert "format-eq" in link.basis.signals, (
        "the base58 example did not reach the signature — the examples-list channel is "
        "not wired into the value-domain corroborator"
    )


# --- the CONFIRM gate, asserted BOTH ways (§13.6) --------------------------------
def test_unconfirmed_cross_api_mutate_is_a_candidate_never_plan_eligible() -> None:
    """Provider-declared-only (x-gecko, no customer confirm): the mutate join is a
    quarantined CANDIDATE. An untrusted provider self-declaration cannot mint an
    executable cross-system mutate."""
    res = correlate_surfaces(_oracle(confirm=False), _mgr(confirm=False))
    link = _mutate_link(res)
    assert link is not None
    assert link.plan_eligible is False
    assert link.candidate is True
    assert "provider-declared-unconfirmed" in link.basis.signals
    # no fabricated join: NOTHING is plan-eligible across the boundary without a confirm.
    assert res.summary["plan_eligible"] == 0


def test_confirmed_cross_api_mutate_becomes_plan_eligible() -> None:
    """Only a CUSTOMER confirm on both sides lifts the same mutate join to plan-eligible."""
    res = correlate_surfaces(_oracle(confirm=True), _mgr(confirm=True))
    link = _mutate_link(res)
    assert link is not None
    assert link.plan_eligible is True
    assert "confirmed" in link.basis.signals
    assert res.summary["plan_eligible"] >= 1


def test_one_sided_confirm_is_not_enough() -> None:
    """Confirming only the consumer (not the producer) must NOT make the cross-API mutate
    plan-eligible — both sides must be customer-vouched."""
    res = correlate_surfaces(_oracle(confirm=False), _mgr(confirm=True))
    link = _mutate_link(res)
    assert link is not None
    assert link.plan_eligible is False
    assert res.summary["plan_eligible"] == 0


# --- the same gate through the value-domain index (vindex must agree) ------------
def test_vindex_cross_join_gate_matches_correlate() -> None:
    """The value-domain index reports the SAME mutate cross-join and the SAME eligibility
    verdict as correlate — the index and the scorer can never disagree (§13 Phase 3.3)."""
    unconfirmed = value_domain_index([_oracle(confirm=False), _mgr(confirm=False)])
    joins_u = unconfirmed.find_correlations(_POOL_NORM)
    mutate_u = [
        j
        for j in joins_u
        if j.producer.op_id == "getPool" and j.consumer.op_id == "openPosition"
    ]
    assert mutate_u, "vindex did not surface the response->body mutate cross-join"
    assert all(j.plan_eligible is False for j in mutate_u)  # candidate, unconfirmed

    confirmed = value_domain_index([_oracle(confirm=True), _mgr(confirm=True)])
    joins_c = confirmed.find_correlations(_POOL_NORM)
    mutate_c = [
        j
        for j in joins_c
        if j.producer.op_id == "getPool" and j.consumer.op_id == "openPosition"
    ]
    assert mutate_c and all(j.plan_eligible for j in mutate_c)
