"""The Jito showcase, falsifiable: these tests pin the demo's claims to the engine.

Offline / $0 — recorded mode only, no network, no keys, no lamports."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from examples.jito_demo.demo import SPEC, TASKS, build_report, main  # noqa: E402
from gecko.access import public_session  # noqa: E402
from gecko.agentnative import build_artifacts  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402


def test_surface_counts_all_five_methods_usable() -> None:
    r = build_report()
    assert r.ops_total == 5
    assert (
        r.surfaced == 5
    )  # uuid auth is optional -> nothing hidden on a public session


def test_scorecard_is_all_first_call_correct() -> None:
    card = build_report().card
    assert card["top1_rate"] == 1.0
    assert card["top5_rate"] == 1.0
    assert card["well_formed_rate"] == 1.0


def test_sendbundle_hits_the_real_route_with_method_in_body() -> None:
    """The JSON-RPC gotcha: the wire target is Jito's real path, and the method rides
    in the body — the exact thing an agent reading prose gets wrong."""
    client = AgentApiClient(SPEC, session=public_session())
    res = client.call("sendBundle", TASKS[0]["args"], mode="recorded")
    assert res["method"] == "POST"
    assert res["request"] == "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
    # recorded response synthesized from the schema: a JSON-RPC envelope
    assert set(res["data"]).issuperset({"jsonrpc", "result"})


def test_status_near_dup_pair_disambiguates() -> None:
    """The classic near-dup: landed-status vs in-flight-status must not swap."""
    client = AgentApiClient(SPEC, session=public_session())
    landed = client.search("did my bundle land on chain and in which slot", limit=1)
    inflight = client.search("track my bundle while it is still in flight", limit=1)
    assert landed[0]["name"] == "getBundleStatuses"
    assert inflight[0]["name"] == "getInflightBundleStatuses"


def test_surface_is_not_quarantined() -> None:
    """The reviewed spec must comprehend clean — no anti-poisoning quarantine (the
    from-docs DRAFT is born quarantined by design; the human-reviewed spec is not)."""
    client = AgentApiClient(SPEC, session=public_session())
    assert client.anchor.state != "quarantined"


def test_agentnative_artifacts_emit_for_jito() -> None:
    """Phase-3 emit works on the Jito surface — the provider hand-off files."""
    client = AgentApiClient(SPEC, session=public_session())
    arts = build_artifacts(client, site_url="https://docs.jito.wtf")
    assert "sendBundle" in arts["tools.md"]
    assert "POST /api/v1/bundles" in arts["llms.txt"]
    import json

    manifest = json.loads(arts["gecko.json"])
    assert manifest["operations"] == 5 and manifest["tools"] == 5


def test_main_prints_offline_without_error(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    out = capsys.readouterr().out
    assert "5 operations -> 5 agent tools" in out
    assert "api/v1/bundles" in out
