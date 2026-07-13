"""Gecko comprehension of TxLINE is first-call-correct — offline, $0, no LLM.

The claim the whole example rests on: point Gecko at the paywalled TxLINE spec and the
odds tools it hands the agent are correctly shaped and correctly called on the first try,
provable without a key or a live call.
"""

from __future__ import annotations

from pathlib import Path

from gecko import AgentApiClient
from gecko.access import public_session, stub_session

from examples.txline_sharp_agent.surfcall_tools import ODDS_READS, TxlineTools

SPEC = str(
    Path(__file__).resolve().parents[2] / "txline_demo" / "spec" / "txline_openapi.yaml"
)


def test_auth_gated_odds_tools_hidden_without_a_session():
    """A no-auth session sees only the public guest-start op — the odds reads are hidden
    because the agent can't satisfy their two-token auth (it can't mis-call what it can't see)."""
    client = AgentApiClient(SPEC, session=public_session())
    names = {t["name"] for t in client.list_tools()}
    assert names & ODDS_READS == set()
    assert "postAuthGuestStart" in names


def test_stub_session_unlocks_the_odds_reads_for_recorded_mode():
    client = AgentApiClient(SPEC, session=stub_session())
    names = {t["name"] for t in client.list_tools()}
    assert ODDS_READS <= names  # all three odds reads present


def test_odds_snapshot_call_is_well_formed_recorded():
    client = AgentApiClient(SPEC, session=stub_session())
    result = client.call(
        "getApiOddsSnapshotFixtureid", {"fixtureId": 42}, mode="recorded"
    )
    assert result["status"] == 200
    assert result["mode"] == "recorded"
    assert result["method"] == "GET"
    assert "/api/odds/snapshot/42" in result["request"]


def test_tool_provider_exposes_only_the_allowlist():
    tools = TxlineTools(SPEC)  # defaults: recorded + stub session + ODDS_READS
    assert tools.tool_names == ODDS_READS
    # a non-allow-listed op is refused, never executed, never crashes
    out = tools.call("postApiTokenActivate", {})
    assert "not allowed" in out
