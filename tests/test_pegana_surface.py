"""Pegana P0 — comprehend + verify the peg-risk oracle surface, offline (Pattern B).

Pegana (``api.pegana.xyz``) is our first live third-party design-partner API. Its public
reads are KEYLESS, so live verify is $0 (proven separately). These tests are the
offline-falsifiable baseline: they run entirely off the COMMITTED, faithful fixture
(``tests/fixtures/pegana_p0_openapi.json`` — the CURRENT live Pegana surface as it ships,
no Gecko enrichment; distinct from the older ``pegana_openapi.json`` API-#2 snapshot) and
never touch the network. They assert:

  * the surface comprehends (every op → a usable tool);
  * the five core read tools are present and shaped right (path params named correctly,
    NO auth header ever exposed — Pegana's reads need none);
  * the recorded verify path yields a sane, control-plane-clean verdict object; and
  * a Pattern-B observed 200 VERIFIES / an observed 404 REFUTES the same op offline
    (the falsifiable hinge — flip the status and the verdict flips).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from gecko import corpus, ingest, tools as tools_mod, verify
from gecko.access import NoAuthSession, public_session
from gecko.client import AgentApiClient

_FIXTURE = Path(__file__).parent / "fixtures" / "pegana_p0_openapi.json"

# The five core keyless reads the P0 plan names, with the path param an agent must name
# right on the first call. ``None`` = the op takes no path param.
_CORE_READS: dict[str, tuple[str, str, str | None]] = {
    "/v1/assets": ("GET", "list_assets", None),
    "/v1/assets/{symbol}/state": ("GET", "state", "symbol"),
    "/v1/assets/by-mint/{mint}": ("GET", "detail_by_mint", "mint"),
    "/v1/assets/{symbol}/simulate-depeg": ("GET", "asset_simulate_depeg", "symbol"),
    "/v1/peg/feed": ("GET", "peg_feed", None),
}


def _spec() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _client() -> AgentApiClient:
    # public_session = the no-auth adapter for Pegana's keyless reads; it also hides the
    # JWT-gated /v1/me/* ops (an agent with no login can't satisfy them).
    return AgentApiClient(_spec(), session=public_session())


def test_surface_comprehends_every_operation() -> None:
    ops = ingest.extract_operations(_spec())
    # 43 operations across 39 paths — the whole surface ingests, none dropped.
    assert len(ops) == 43
    # Every op that a keyless session can use is surfaced as a tool.
    keyless = [o for o in ops if not tools_mod._security_requires_auth(o)]
    assert len(_client().list_tools()) == len(keyless) == 28


def test_core_read_tools_present_and_shaped_right() -> None:
    ops = {o.path: o for o in ingest.extract_operations(_spec())}
    for path, (method, expected_name, path_param) in _CORE_READS.items():
        assert path in ops, f"core read {path} missing from the surface"
        op = ops[path]
        assert op.method == method
        assert tools_mod.tool_name(op) == expected_name
        # Keyless: Pegana's public reads carry no security requirement.
        assert tools_mod._security_requires_auth(op) is False
        schema = tools_mod._input_schema(op)
        props = schema.get("properties", {})
        if path_param is not None:
            # the path param is named correctly and required — the first-call hinge.
            assert path_param in props
            assert path_param in schema.get("required", [])
        # NO auth header is ever exposed to the agent (invariant #4), on any core read.
        assert not any(tools_mod._is_auth_param(p) for p in op.parameters)
        assert not any(
            k.lower() in {"authorization", "x-api-key", "api_key", "apikey", "token"}
            for k in props
        )


def test_state_tool_names_symbol_and_hides_no_auth() -> None:
    """The plan's explicit sanity check: GET /v1/assets/{symbol}/state names the `symbol`
    path param and hides no auth (there is none)."""
    op = next(
        o
        for o in ingest.extract_operations(_spec())
        if o.path == "/v1/assets/{symbol}/state"
    )
    assert [(p.name, p.location, p.required) for p in op.parameters] == [
        ("symbol", "path", True)
    ]
    assert op.security == []
    assert tools_mod._security_requires_auth(op) is False


def test_recorded_verify_is_sane_and_control_plane_clean() -> None:
    """Offline recorded verify: every op honestly UNVERIFIED (no wire evidence), and the
    verdict object carries only control-plane fields — never a response payload."""
    result = verify.verify_docs(_client(), mode="recorded")
    assert result["mode"] == "recorded"
    assert result["summary"]["verified"] == 0
    assert result["summary"]["refuted"] == 0
    assert result["summary"]["unverified"] > 0
    for entry in result["report"].values():
        assert set(entry) == {"status", "basis", "provenance"}
        assert entry["status"] == "UNVERIFIED"
        assert all(isinstance(b, str) for b in entry["basis"])


def _observed(client: AgentApiClient, op_id: str, status: int) -> corpus.CallOutcome:
    """A control-plane-safe outcome whose ``source`` derives to 'observed' (mode='live'),
    the only source the honesty gate will let VERIFY/REFUTE — status only, no body."""
    op = next(o for o in ingest.extract_operations(_spec()) if o.operation_id == op_id)
    return corpus.outcome_from(
        operation_id=op_id,
        tool_invoke={"method": op.method, "path": op.path},
        args={"symbol": "USDC"},
        status=status,
        error_class=corpus.error_class_for(status, None),
        latency_ms=None,
        mode="live",
        auth_injected=False,
        ts=int(time.time() * 1000),
        surface_id=client.surface_id,
        surface_rev=client.surface_rev,
    )


def test_observed_200_verifies_and_404_refutes_offline() -> None:
    """Pattern B, falsifiable: a real 200 replayed offline VERIFIES the `state` read; flip
    it to 404 and the SAME op REFUTES. No network — the status is the only input."""
    client = _client()

    ok = verify.verify_docs(
        client, mode="recorded", outcomes={"state": _observed(client, "state", 200)}
    )
    assert ok["report"]["state"]["status"] == "VERIFIED"
    assert ok["report"]["state"]["basis"] == ["replay:200"]

    gone = verify.verify_docs(
        client, mode="recorded", outcomes={"state": _observed(client, "state", 404)}
    )
    assert gone["report"]["state"]["status"] == "REFUTED"


def test_mutating_ops_never_probed_live_is_moot_here_but_guarded() -> None:
    """Sanity: Pegana's keyless surface has 3 POST auth-flow ops. A no-auth session hides
    them, and even if surfaced the verify path never fires a non-GET live (do-no-harm).
    Here we assert the no-auth surface simply excludes them — nothing to mutate."""
    client = AgentApiClient(_spec(), session=NoAuthSession())
    names = {t["name"] for t in client.list_tools()}
    # the JWT-gated me/* + logout ops are hidden; only keyless ops remain.
    assert "logout" not in names
    assert all(not t.get("requires_auth") for t in client.list_tools())
