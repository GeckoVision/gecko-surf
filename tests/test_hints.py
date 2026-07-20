"""DECLARED hint tests (§13.2 top of ladder): x-gecko spec parsing (untrusted →
sanitized, capped, fail-quiet-drop), per-surface confirmed persistence with the
audit trail, and the client wiring (spec hints + injected confirmations → the
graph's DECLARED edges). Offline, $0."""

from __future__ import annotations

import json
import stat
from typing import Any

import pytest

from gecko.client import AgentApiClient
from gecko.hints import (
    confirm_entity,
    declared_entity_hints,
    list_confirmed,
    load_confirmed,
    remove_confirmed,
)


# --- spec parsing ----------------------------------------------------------------
def test_root_entities_block() -> None:
    spec = {"x-gecko": {"entities": {"FixtureId": "Fixture", "seq": "sequence"}}}
    assert declared_entity_hints(spec) == {"FixtureId": "fixture", "seq": "sequence"}


def test_inline_param_and_property_markers() -> None:
    spec = {
        "paths": {
            "/odds": {
                "get": {
                    "parameters": [
                        {
                            "name": "fixture_ref",
                            "in": "query",
                            "x-gecko-entity": "fixture",
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "properties": {
                                            "FixtureId": {
                                                "type": "integer",
                                                "x-gecko-entity": "fixture",
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    },
                }
            }
        }
    }
    hints = declared_entity_hints(spec)
    assert hints == {"fixture_ref": "fixture", "FixtureId": "fixture"}


def test_hostile_hints_are_dropped_not_raised() -> None:
    spec: dict[str, Any] = {
        "x-gecko": {
            "entities": {
                "ok": "fine",
                "bad_entity": "../../etc/passwd",
                "bad entity": "spaces are out",
                "": "empty-name",
                "x" * 300: "name-too-long",
                "injection": "e" * 100,
                "nonstr": 42,
            }
        }
    }
    assert declared_entity_hints(spec) == {"ok": "fine"}


def test_hint_bomb_is_capped() -> None:
    spec = {"x-gecko": {"entities": {f"name{i}": "ent" for i in range(10_000)}}}
    assert len(declared_entity_hints(spec)) <= 256


def test_non_dict_spec_is_empty() -> None:
    assert declared_entity_hints(None) == {}
    assert declared_entity_hints("nope") == {}


# --- confirmed persistence -------------------------------------------------------
@pytest.fixture(autouse=True)
def _gecko_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_confirm_load_remove_roundtrip(_gecko_home) -> None:
    rec = confirm_entity("txline", "FixtureId", "Fixture", prior_basis="entity:fixture")
    assert rec["entity"] == "fixture" and rec["prior_basis"] == "entity:fixture"
    assert load_confirmed("txline") == {"FixtureId": "fixture"}
    # the file is private (0600) and holds names/entities only — never traffic
    path = _gecko_home / "declared" / "txline.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert set(stored["hints"][0]) <= {
        "name",
        "entity",
        "prior_basis",
        "confirmed_at",
        "history",
    }
    assert remove_confirmed("txline", "FixtureId") is True
    assert load_confirmed("txline") == {}
    assert remove_confirmed("txline", "FixtureId") is False  # idempotent


def test_reconfirm_keeps_history(_gecko_home) -> None:
    confirm_entity("s", "ref", "fixture")
    rec = confirm_entity("s", "ref", "match", prior_basis="declared:fixture")
    assert rec["entity"] == "match"
    assert rec["history"] and rec["history"][0]["entity"] == "fixture"
    assert load_confirmed("s") == {"ref": "match"}
    assert len(list_confirmed("s")) == 1


def test_surface_id_traversal_blocked(_gecko_home) -> None:
    with pytest.raises(ValueError):
        confirm_entity("../evil", "a", "b")
    assert load_confirmed("../evil") == {}  # loading fails quiet, never raises


# --- client wiring ---------------------------------------------------------------
def _spec_with_synonyms(with_xgecko: bool) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "paths": {
            "/fixtures": {
                "get": {
                    "operationId": "listFixtures",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "FixtureId": {"type": "integer"}
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/markets": {
                "get": {
                    "operationId": "openMarket",
                    "parameters": [
                        {
                            "name": "fixture_ref",
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
    if with_xgecko:
        spec["x-gecko"] = {
            "entities": {"FixtureId": "fixture", "fixture_ref": "fixture"}
        }
    return spec


def test_client_builds_declared_edges_from_spec_hints() -> None:
    client = AgentApiClient(_spec_with_synonyms(True), surface_id="test")
    g = client.surface_graph
    declared = [e for e in g.edges if e.provenance == "DECLARED"]
    assert declared and declared[0].basis == "declared:fixture"


def test_client_without_hints_has_no_declared_edges() -> None:
    client = AgentApiClient(_spec_with_synonyms(False), surface_id="test")
    assert not [e for e in client.surface_graph.edges if e.provenance == "DECLARED"]


def test_injected_confirmation_wins_over_spec_hint() -> None:
    spec = _spec_with_synonyms(True)
    client = AgentApiClient(
        spec, surface_id="test", declared_hints={"fixture_ref": "other"}
    )
    g = client.surface_graph
    # the customer moved fixture_ref to a different entity -> the spec's join is gone
    assert not [e for e in g.edges if e.provenance == "DECLARED"]
    assert ("fixtureref", "other") in g.declared
