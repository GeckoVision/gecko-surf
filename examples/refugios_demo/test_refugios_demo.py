"""Pin the Refugios showcase + the publishable-key auth gating. Offline / $0."""

from __future__ import annotations

from pathlib import Path

from examples.refugios_demo.demo import SPEC, build_report
from gecko.access import public_session, static_session
from gecko.client import AgentApiClient


def test_one_op_surfaces_as_one_tool() -> None:
    r = build_report()
    assert r.ops_total == 1
    assert r.surfaced == 1


def test_publishable_key_hidden_from_agent() -> None:
    assert build_report().apikey_hidden


def test_well_formed_first_call() -> None:
    assert build_report().card["well_formed_rate"] == 1.0


def test_service_filters_route_to_list() -> None:
    c = AgentApiClient(SPEC, session=static_session({"apikey": "x"}))
    for q in [
        "refugio con agua y atención médica",
        "shelters that accept pets",
        "un refugio abierto en Caracas",
    ]:
        assert c.search(q, limit=1)[0]["name"] == "listRefugios"


def test_gated_hidden_without_the_key() -> None:
    # public_session (no auth) hides the apikey-gated op — proves the key is
    # load-bearing and that Gecko never offers a tool it can't satisfy (invariant #4).
    assert len(AgentApiClient(SPEC, session=public_session()).list_tools()) == 0


def test_spec_ships_in_repo() -> None:
    assert Path(SPEC).is_file()
