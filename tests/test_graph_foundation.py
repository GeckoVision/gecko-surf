"""Step-3 foundation tests (§12 Phase 3, §13 post-probe): surface_id namespacing,
the §13.1 value-domain signature on nodes, signature-as-corroborator (`+sig`),
DECLARED in the provenance ladder, and the ladder-ranked planning tiebreak.

All specs are inline fixtures (extract_operations takes a dict) — offline, $0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gecko.graph import build_graph, plan
from gecko.ingest import extract_operations, load_spec

FIX = Path(__file__).resolve().parent / "fixtures"
TXLINE = FIX / "txline_openapi.yaml"


def _ops(spec: dict[str, Any]):
    return extract_operations(spec)


def _mini_spec(
    *,
    field_schema: dict[str, Any] | None = None,
    param_schema: dict[str, Any] | None = None,
    field_name: str = "fixtureId",
    param_name: str = "fixtureId",
) -> dict[str, Any]:
    """Two ops: a producer whose 200 emits ``field_name``, and a consumer whose
    required query param is ``param_name`` — the smallest chain-able surface."""
    return {
        "openapi": "3.0.0",
        "paths": {
            "/fixtures/snapshot": {
                "get": {
                    "operationId": "listFixtures",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            field_name: field_schema
                                            or {"type": "integer"}
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/odds/updates": {
                "get": {
                    "operationId": "getOdds",
                    "parameters": [
                        {
                            "name": param_name,
                            "in": "query",
                            "required": True,
                            "schema": param_schema or {"type": "integer"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


# --- surface_id namespacing ------------------------------------------------------
def test_namespaced_ids_and_planstep_surface() -> None:
    ops = _ops(_mini_spec())
    g = build_graph(ops, surface_id="txline")
    assert all(n.id.startswith("txline::") for n in g.nodes)
    p = plan(g, "getOdds", set())
    assert p is not None
    assert [s.operation_id for s in p.steps] == ["listFixtures", "getOdds"]
    assert all(s.surface == "txline" for s in p.steps)


def test_empty_surface_id_keeps_legacy_ids() -> None:
    ops = _ops(_mini_spec())
    g = build_graph(ops)
    assert g.surface_id == ""
    assert any(n.id == "op:getOdds" for n in g.nodes)
    p = plan(g, "getOdds", set())
    assert p is not None and p.steps[0].surface == ""


def test_surface_id_is_one_way_in_content_hash() -> None:
    ops = _ops(_mini_spec())
    assert (
        build_graph(ops).content_hash()
        != build_graph(ops, surface_id="txline").content_hash()
    )
    # deterministic: same inputs -> same hash
    assert (
        build_graph(ops, surface_id="txline").content_hash()
        == build_graph(ops, surface_id="txline").content_hash()
    )


# --- the §13.1 signature on nodes ------------------------------------------------
def test_value_domain_signature_captured_on_nodes() -> None:
    spec = _mini_spec(
        field_schema={"type": "string", "pattern": "^[A-Z]{3}$", "format": "currency"},
        param_schema={"type": "string", "enum": ["EUR", "USD"]},
    )
    g = build_graph(_ops(spec))
    fld = next(n for n in g.nodes if n.kind == "field")
    prm = next(n for n in g.nodes if n.kind == "param")
    t, fmt, pat8, en8 = fld.sig.split("|")
    assert (t, fmt) == ("string", "currency") and len(pat8) == 8
    t2, _, _, en8b = prm.sig.split("|")
    assert t2 == "string" and len(en8b) == 8


def test_signature_feeds_content_hash() -> None:
    plain = _mini_spec()
    constrained = _mini_spec(field_schema={"type": "integer", "format": "int64"})
    assert (
        build_graph(_ops(plain)).content_hash()
        != build_graph(_ops(constrained)).content_hash()
    )


# --- signature as corroborator (+sig), never a standalone basis ------------------
def test_sig_corroborates_entity_match() -> None:
    pat = "^FIX-[0-9]{6}$"
    spec = _mini_spec(
        field_schema={"type": "string", "pattern": pat},
        param_schema={"type": "string", "pattern": pat},
    )
    g = build_graph(_ops(spec))
    e = next(e for e in g.edges if e.kind == "feeds")
    assert e.provenance == "INFERRED"
    assert e.basis == "entity:fixture+sig" and e.confidence == "high"


def test_bare_same_type_is_not_corroboration() -> None:
    # both plain integers — the §13.6 signal-free case: entity match stands, no +sig
    g = build_graph(_ops(_mini_spec()))
    e = next(e for e in g.edges if e.kind == "feeds")
    assert e.basis == "entity:fixture"


def test_matching_sig_alone_mints_no_edge() -> None:
    # same rare pattern on BOTH sides but unrelated names/entities: the signature
    # is a corroborator only (§13.6) — no feeds edge may exist at all.
    pat = "^[a-f0-9]{64}$"
    spec = _mini_spec(
        field_schema={"type": "string", "pattern": pat},
        param_schema={"type": "string", "pattern": pat},
        field_name="contentDigest",
        param_name="anchorRef",
    )
    g = build_graph(_ops(spec))
    assert not [e for e in g.edges if e.kind == "feeds"]


# --- DECLARED in the ladder ------------------------------------------------------
def test_declared_hint_joins_synonym_names() -> None:
    """The §13.6 headline case: producer field and consumer param share an entity
    ONLY via explicit hints (names differ, no signature) -> DECLARED high edge."""
    spec = _mini_spec(
        field_schema={"type": "string"},
        param_schema={"type": "string"},
        field_name="FixtureId",
        param_name="fixture_ref",
    )
    g = build_graph(
        _ops(spec), declared={"FixtureId": "fixture", "fixture_ref": "fixture"}
    )
    e = next(e for e in g.edges if e.kind == "feeds")
    assert e.provenance == "DECLARED"
    assert e.basis == "declared:fixture" and e.confidence == "high"
    # and it is plan-eligible: the chain resolves through it
    p = plan(g, "getOdds", set())
    assert p is not None
    fed = [x for x in p.explain if x.param == "fixture_ref"]
    assert fed and fed[0].provenance == "DECLARED"
    # the declared vocabulary rides the graph (for compose) and the hash
    assert ("fixtureid", "fixture") in g.declared
    assert build_graph(_ops(spec)).content_hash() != g.content_hash()


def test_declared_hint_on_boolean_mints_nothing() -> None:
    spec = _mini_spec(
        field_schema={"type": "boolean"},
        param_schema={"type": "string"},
        field_name="settled",
        param_name="settled_flag",
    )
    g = build_graph(_ops(spec), declared={"settled": "x", "settled_flag": "x"})
    assert not [e for e in g.edges if e.kind == "feeds"]


def test_ladder_prefers_declared_over_inferred_at_equal_cost() -> None:
    """Two suppliers for the same param at equal cost: one via INFERRED name-entity
    match, one via DECLARED hint — the §13.2 ladder picks DECLARED."""
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "paths": {
            "/a": {
                "get": {
                    "operationId": "opInferred",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "fixtureId": {"type": "integer"}
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/b": {
                "get": {
                    "operationId": "opDeclared",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"FixRef": {"type": "integer"}},
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/c": {
                "get": {
                    "operationId": "opConsumer",
                    "parameters": [
                        {
                            "name": "fixtureId",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    g = build_graph(_ops(spec), declared={"FixRef": "fixture", "fixtureId": "fixture"})
    p = plan(g, "opConsumer", set())
    assert p is not None
    fed = [x for x in p.explain if x.param == "fixtureId"]
    assert fed and fed[0].provenance == "DECLARED" and fed[0].source_op == "opDeclared"


# --- the real spec still behaves (regression anchor) -----------------------------
def test_txline_chain_survives_namespacing() -> None:
    ops = extract_operations(load_spec(str(TXLINE)))
    g = build_graph(ops, surface_id="txline")
    p = plan(g, "getApiOddsUpdatesFixtureid", set())
    assert p is not None
    assert p.steps[-1].operation_id == "getApiOddsUpdatesFixtureid"
    assert all(s.surface == "txline" for s in p.steps)
    fed = [e for e in p.explain if e.param == "fixtureId"]
    assert fed and fed[0].provenance == "INFERRED"
    assert fed[0].basis.startswith("entity:fixture")
