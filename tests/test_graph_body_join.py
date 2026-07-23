"""V2.1 — body-carried join keys.

The gap (correlation roadmap V2.1): a join key that lives only in a request BODY, not a
URL path/query param, was invisible to the planner — so a create→create chain
(`createOrder` produces `orderId` → `createShipment` needs `orderId` in its body) could
not be planned. This is most of the interesting *mutate* work.

Security note (the reason this went through defi-security-engineer): a request body's
field schema — its `default`/`example`/`enum` — is attacker-controllable exactly like a
query param's. A body-derived join key must therefore be INFERRED (never EXTRACTED as a
feed), must pass the same id-shape + genericity + entity gates, and only REQUIRED body
scalars are surfaced as plannable inputs (an optional body field never blocks a first
call, so it is never a chain-critical target).
"""

from __future__ import annotations

from pathlib import Path

from gecko.graph import build_graph, plan
from gecko.ingest import extract_operations, load_spec

FIX = Path(__file__).resolve().parent / "fixtures"
BODY_JOIN = FIX / "graph_body_join.json"


def _graph(path: Path):
    return build_graph(extract_operations(load_spec(str(path))))


# The realistic call: the agent's intent supplies the inputs it NAMES (here `address`,
# from "ship this order to <address>"); the planner sources what it doesn't have
# (`orderId`). This mirrors the TxLINE chain2 test, which supplies fixtureId+statKey and
# lets the planner source `seq`. Passing an empty set would mean "the agent provides
# nothing", which no real intent does.


def test_body_carried_join_key_plans_the_create_to_create_chain() -> None:
    """createShipment needs `orderId` in its body; the planner sources it from
    createOrder's produced `orderId`. The whole point of V2.1."""
    g = _graph(BODY_JOIN)
    p = plan(g, "createShipment", {"address"})
    assert p is not None, "no plan — the body join key was not modelled"
    assert p.steps[-1].operation_id == "createShipment"

    supplier = p.steps[0]
    assert supplier.operation_id == "createOrder"
    assert "orderId" in supplier.supplies

    fed = [e for e in p.explain if e.param == "orderId"]
    assert fed, "orderId was not sourced"
    # id-shape + entity match, INFERRED (never EXTRACTED for a derived feed).
    assert fed[0].provenance == "INFERRED"
    assert fed[0].source_op == "createOrder"
    assert fed[0].source_field == "orderId"


def test_no_plan_when_the_body_join_key_is_already_in_hand() -> None:
    """If the intent already names orderId (and address), no supplier chain is invented."""
    g = _graph(BODY_JOIN)
    p = plan(g, "createShipment", {"orderId", "address"})
    assert p is not None
    assert len(p.steps) == 1
    assert p.explain == ()


def test_a_required_body_field_is_a_consumed_input_an_optional_one_is_not() -> None:
    """`address` (required) is a consumed input; `note` (optional) is never chain-critical
    and must not appear — optional body fields don't block a first call."""
    g = _graph(BODY_JOIN)
    p = plan(g, "createShipment", {"address"})
    assert p is not None
    consumed = {c for step in p.steps for c in step.consumes}
    assert "orderId" in consumed  # required body join key, sourced
    assert "address" in consumed  # required body field, agent-supplied
    assert "note" not in consumed  # optional — never surfaced


def test_a_body_join_key_gets_identical_treatment_to_a_query_param() -> None:
    """V2.1's guarantee is PARITY: a body join key is neither stronger nor weaker than
    the same key as a query param — same INFERRED feeds rules (entity match, genericity,
    id-shape). Built once as a body field and once as a query param; the plans match."""
    producer = {
        "operationId": "createOrder",
        "responses": {
            "200": {
                "description": "ok",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"orderId": {"type": "string"}},
                        }
                    }
                },
            }
        },
    }
    base = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}}
    body_spec = {
        **base,
        "paths": {
            "/orders": {"post": producer},
            "/ship": {
                "post": {
                    "operationId": "ship",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["orderId"],
                                    "properties": {"orderId": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    query_spec = {
        **base,
        "paths": {
            "/orders": {"post": producer},
            "/ship": {
                "get": {
                    "operationId": "ship",
                    "parameters": [
                        {
                            "name": "orderId",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }

    def summary(p):
        assert p is not None
        return [(e.param, e.source_op, e.source_field, e.provenance) for e in p.explain]

    body_plan = plan(build_graph(extract_operations(body_spec)), "ship", set())
    query_plan = plan(build_graph(extract_operations(query_spec)), "ship", set())
    assert summary(body_plan) == summary(query_plan)


def test_a_body_join_key_is_never_extracted_only_inferred_or_declared() -> None:
    """A feeds edge into a body param must never be EXTRACTED — a poisoned body schema
    can at worst mint an auditable INFERRED/DECLARED edge, never a spec-stated fact."""
    g = _graph(BODY_JOIN)
    body_param_ids = {
        n.id for n in g.nodes if n.kind == "param" and n.detail.startswith("body|")
    }
    body_feeds = [e for e in g.edges if e.kind == "feeds" and e.dst in body_param_ids]
    assert body_feeds, "expected at least one feeds edge into a body param"
    assert all(e.provenance in ("INFERRED", "DECLARED") for e in body_feeds)
