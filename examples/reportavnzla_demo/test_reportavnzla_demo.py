"""Pin the ReportaVNZLA showcase to the engine. Offline / $0, deterministic."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from examples.reportavnzla_demo.demo import SPEC, build_report  # noqa: E402
from gecko.access import public_session  # noqa: E402
from gecko.agentnative import build_artifacts  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402


def test_all_four_read_ops_usable_no_token() -> None:
    r = build_report()
    assert r.ops_total == 4
    assert r.surfaced == 4  # public reads -> nothing hidden


def test_scorecard_is_first_call_correct() -> None:
    card = build_report().card
    assert card["top1_rate"] == 1.0
    assert card["well_formed_rate"] == 1.0


def test_status_filter_and_search_disambiguate() -> None:
    client = AgentApiClient(SPEC, session=public_session())
    assert (
        client.search("how many people are missing", limit=1)[0]["name"] == "getStats"
    )
    assert (
        client.search("find donation collection centers", limit=1)[0]["name"]
        == "listRecursos"
    )
    assert (
        client.search("search for a missing person named Ana", limit=1)[0]["name"]
        == "searchPersonas"
    )


def test_surface_is_not_quarantined() -> None:
    client = AgentApiClient(SPEC, session=public_session())
    assert client.anchor.state != "quarantined"


def test_person_carries_coordinates_for_nearest_place() -> None:
    client = AgentApiClient(SPEC, session=public_session())
    props = client.spec["components"]["schemas"]["Person"]["properties"]
    assert "lat" in props and "lng" in props


def test_agentnative_emit_for_reportavnzla() -> None:
    client = AgentApiClient(SPEC, session=public_session())
    arts = build_artifacts(client, site_url="https://reportavnzla.com")
    assert "searchPersonas" in arts["tools.md"]
    manifest = json.loads(arts["gecko.json"])
    assert manifest["operations"] == 4 and manifest["tools"] == 4
