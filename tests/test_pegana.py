"""Pegana — the second painful API, ingested unilaterally from its real OpenAPI.

Proves the API-agnostic engine on an API that is NOT TxODDS and NOT in any catalog,
and that the op.security fix hides the 15 telegram_jwt ops from a public (no-auth)
session. Recorded/offline — the spec is a committed fixture (surface only).
"""

from pathlib import Path

import pytest

from gecko.access import public_session
from gecko.caller import CallError, build_request
from gecko.client import AgentApiClient
from gecko.evaluate import evaluate_tasks
from gecko.ingest import extract_operations, load_spec
from gecko.tools import to_tool

FIXTURE = Path(__file__).parent / "fixtures" / "pegana_openapi.json"
JITO_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"


def _ops():
    return extract_operations(load_spec(str(FIXTURE)))


def test_ingests_full_surface():
    assert len(_ops()) == 41


def test_state_by_mint_present_and_public():
    op = next(o for o in _ops() if o.operation_id == "state_by_mint")
    assert op.method == "GET"
    assert op.path == "/v1/assets/by-mint/{mint}/state"
    assert to_tool(op)["requires_auth"] is False


def test_healthz_present_and_health_absent():
    paths = {o.path for o in _ops()}
    assert "/healthz" in paths
    assert "/health" not in paths  # the documented 404 trap


def test_build_request_for_state_by_mint_uses_the_mint():
    op = next(o for o in _ops() if o.operation_id == "state_by_mint")
    req = build_request(
        to_tool(op), {"mint": JITO_MINT}, base_url="https://api.pegana.xyz", auth={}
    )
    assert req.method == "GET"
    assert f"/v1/assets/by-mint/{JITO_MINT}/state" in req.url


def test_public_session_hides_all_me_endpoints():
    client = AgentApiClient(str(FIXTURE), session=public_session())
    surfaced = client.list_tools()
    assert surfaced, "expected public tools to be surfaced"
    assert all(not t.get("requires_auth") for t in surfaced)
    paths = {t["_invoke"]["path"] for t in surfaced}
    # the user namespace /v1/me and /v1/me/* (NOT /v1/meta or /v1/methodology, which are public)
    assert not any(p == "/v1/me" or p.startswith("/v1/me/") for p in paths)
    names = {t["name"] for t in surfaced}
    assert "list_subs" not in names
    assert "me" not in names


def test_state_by_mint_is_retrievable():
    client = AgentApiClient(str(FIXTURE), session=public_session())
    hits = [h["name"] for h in client.search("get peg state by mint address", limit=5)]
    assert "state_by_mint" in hits


def test_prepare_auth_op_raises_under_public_session():
    client = AgentApiClient(str(FIXTURE), session=public_session())
    with pytest.raises(CallError):
        client.prepare("list_subs", {})  # telegram_jwt-gated


def test_evaluate_tasks_runs_and_resolves_key_tasks():
    client = AgentApiClient(str(FIXTURE), session=public_session())
    tasks = [
        {
            "goal": "what is the current peg state for the asset with this mint address",
            "expect_op": "state_by_mint",
            "args": {"mint": JITO_MINT},
        },
        {
            "goal": "aggregate counters and delivery health for the whole universe",
            "expect_op": "summary",
            "args": {},
        },
        {
            "goal": "liveness probe is the service alive",
            "expect_op": "live",
            "args": {},
        },
    ]
    card = evaluate_tasks(client, tasks)
    assert card["well_formed_rate"] == 1.0
    assert card["top5_rate"] == 1.0
