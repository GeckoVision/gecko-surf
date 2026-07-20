"""Step-6b tests: the `gecko graph confirm|declared|rm` CLI (thin transport over
``gecko.hints``) and the full confirm loop — a confirmation persisted on disk
upgrades a client's graph to a DECLARED join on the next build. Offline, $0."""

from __future__ import annotations

from typing import Any

import pytest

from gecko.cli import main
from gecko.client import AgentApiClient
from gecko.hints import load_confirmed


@pytest.fixture(autouse=True)
def _gecko_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))
    return tmp_path


# --- CLI round-trip --------------------------------------------------------------
def test_confirm_declared_rm_roundtrip(capsys) -> None:
    assert main(["graph", "confirm", "txline", "FixtureId", "fixture"]) == 0
    out = capsys.readouterr().out
    assert "FixtureId → fixture" in out and "DECLARED" in out
    assert load_confirmed("txline") == {"FixtureId": "fixture"}

    assert main(["graph", "declared", "txline"]) == 0
    assert "FixtureId → fixture" in capsys.readouterr().out

    assert main(["graph", "rm", "txline", "FixtureId"]) == 0
    assert load_confirmed("txline") == {}
    # idempotent rm
    assert main(["graph", "rm", "txline", "FixtureId"]) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_confirm_audit_basis_recorded(capsys) -> None:
    assert (
        main(
            [
                "graph",
                "confirm",
                "txline",
                "seq",
                "sequence",
                "--basis",
                "rare-key:seq",
            ]
        )
        == 0
    )
    assert main(["graph", "declared", "txline"]) == 0
    assert "upgraded: rare-key:seq" in capsys.readouterr().out


def test_confirm_rejects_malformed(capsys) -> None:
    assert main(["graph", "confirm", "txline", "x", "../../etc"]) == 1
    assert "shape gate" in capsys.readouterr().err


def test_declared_empty_surface(capsys) -> None:
    assert main(["graph", "declared", "nothing-here"]) == 0
    assert "No confirmed mappings" in capsys.readouterr().out


# --- the loop closes: confirm on disk -> DECLARED join in the next graph ---------
def _synonym_spec() -> dict[str, Any]:
    return {
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
            "/odds": {
                "get": {
                    "operationId": "getOdds",
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


def test_confirm_loop_upgrades_client_graph() -> None:
    """The whole §12 loop: no hints -> no join; two confirmations (persisted via
    the CLI) injected at the serve edge -> the DECLARED join appears; and
    add_declared_hints invalidates the cached graph (no stale plans)."""
    client = AgentApiClient(_synonym_spec(), surface_id="myapi")
    g0 = client.surface_graph
    assert not [e for e in g0.edges if e.provenance == "DECLARED"]

    assert main(["graph", "confirm", "myapi", "FixtureId", "fixture"]) == 0
    assert main(["graph", "confirm", "myapi", "fixture_ref", "fixture"]) == 0

    # what serve does after naming the surface (thin edge -> pure client)
    client.add_declared_hints(load_confirmed("myapi"))
    g1 = client.surface_graph
    declared = [e for e in g1.edges if e.provenance == "DECLARED"]
    assert declared and declared[0].basis == "declared:fixture"
    assert g1.content_hash() != g0.content_hash()  # the upgrade is content-addressed
