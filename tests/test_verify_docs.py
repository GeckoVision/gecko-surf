"""VAS-2 — ``gecko verify-docs``: check a surface's ops against reality, attach verdicts.

Pattern B: recorded is the default, offline-falsifiable baseline (every op honestly
UNVERIFIED — no wire evidence); live is proven here with an INJECTED fake transport, never
a real network call. One light fake per test (the transport), per the repo test rules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gecko import verify
from gecko.access import NoAuthSession
from gecko.caller import PreparedRequest
from gecko.cli import main as cli_main
from gecko.client import AgentApiClient

_FIXTURE = Path(__file__).parent / "fixtures" / "tiny_openapi.json"

# a two-op spec (one op the API serves, one a docs source fabricated) for the live case.
_LIVE_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Wallet API", "version": "1"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/balance": {
            "get": {
                "operationId": "getBalance",
                "summary": "Read the account balance.",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"balance": {"type": "number"}},
                                }
                            }
                        }
                    }
                },
            }
        },
        "/ghost": {
            "get": {
                "operationId": "getGhost",
                "summary": "An endpoint a docs source claimed but the API never served.",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                }
                            }
                        }
                    }
                },
            }
        },
    },
}

# a from-docs draft: the info-level marker gecko.docs_reader stamps on every recovered spec.
_DRAFT_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {
        "title": "Recovered API",
        "version": "0.1.0-draft",
        "x-generated-by": "gecko.docs_reader",
    },
    "paths": {
        "/thing": {
            "get": {
                "operationId": "getThing",
                "summary": "A field recovered from human docs.",
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"thing": {"type": "string"}},
                                }
                            }
                        }
                    }
                },
            }
        }
    },
}


def _no_payload_leaked(report: dict[str, Any]) -> None:
    """The report is control-plane clean: only op ids + status + basis strings + a
    provenance category. Assert no synthesized response value rode along."""
    blob = json.dumps(report)
    for entry in report["report"].values():
        assert set(entry) == {"status", "basis", "provenance"}
        assert entry["status"] in {"VERIFIED", "REFUTED", "UNVERIFIED"}
        assert entry["provenance"] in {"CLAIMED", "EXTRACTED", "DECLARED", "INFERRED"}
        assert all(isinstance(b, str) for b in entry["basis"])
    # a placeholder response value ("pong"/"balance"/"thing") must never appear.
    for leaked in ("pong", "balance", "thing", "ghost"):
        assert leaked not in blob.replace("getGhost", "").replace("getBalance", "")


def test_recorded_every_op_unverified_and_control_plane_clean() -> None:
    client = AgentApiClient(str(_FIXTURE), session=NoAuthSession())

    result = verify.verify_docs(client, mode="recorded")

    assert result["mode"] == "recorded"
    assert result["summary"] == {"verified": 0, "refuted": 0, "unverified": 1}
    assert result["report"]["ping"]["status"] == "UNVERIFIED"
    # honesty flag: a recorded 200 is never overclaimed — the basis names the no-access.
    assert result["report"]["ping"]["basis"] == ["no-access:recorded-only"]
    _no_payload_leaked(result)


def test_recorded_is_deterministic() -> None:
    client = AgentApiClient(str(_FIXTURE), session=NoAuthSession())
    first = verify.verify_docs(client, mode="recorded")
    second = verify.verify_docs(client, mode="recorded")
    assert first == second


def test_live_200_verifies_and_404_refutes() -> None:
    """The Privy-fabrication case, proven offline via an injected fake transport: the
    served op VERIFIES on a 200, the docs-fabricated op REFUTES on a 404."""

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        if "/ghost" in req.url:
            return 404, {"error": "not found"}
        return 200, {"balance": 42}

    client = AgentApiClient(
        _LIVE_SPEC,
        base_url="https://api.example.com",
        session=NoAuthSession(),  # nothing to inject -> live never degrades
        live_transport=transport,
    )

    result = verify.verify_docs(client, mode="live")

    assert result["report"]["getBalance"]["status"] == "VERIFIED"
    assert result["report"]["getBalance"]["basis"] == ["replay:200"]
    assert result["report"]["getGhost"]["status"] == "REFUTED"
    # the basis NAMES the 404 (the refutation signal), never a body.
    assert result["report"]["getGhost"]["basis"] == ["replay:404"]
    assert result["summary"] == {"verified": 1, "refuted": 1, "unverified": 0}
    _no_payload_leaked(result)


def test_live_never_fires_a_mutating_method() -> None:
    """Do-no-harm: under ``--live`` a POST op must NOT reach the wire — the verify probe
    downgrades any non-GET/HEAD method to recorded so it can never mutate a live provider.
    The op still gets a verdict (honestly UNVERIFIED — no wire evidence)."""
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {"title": "Mutating API", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/subscribe": {
                "post": {
                    "operationId": "subscribe",
                    "summary": "Create a subscription (mutating — must never fire on a probe).",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    fired: list[str] = []

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        fired.append(req.url)
        return 200, {"ok": True}

    client = AgentApiClient(
        spec,
        base_url="https://api.example.com",
        session=NoAuthSession(),
        live_transport=transport,
    )

    result = verify.verify_docs(client, mode="live")

    assert fired == []  # the POST never reached the wire
    assert result["report"]["subscribe"]["status"] == "UNVERIFIED"


def test_verdict_attaches_to_the_op_node() -> None:
    def transport(req: PreparedRequest) -> tuple[int, Any]:
        return 200, {"balance": 42}

    client = AgentApiClient(
        _LIVE_SPEC,
        base_url="https://api.example.com",
        session=NoAuthSession(),
        live_transport=transport,
    )
    verify.verify_docs(client, mode="live")

    graph = client.surface_graph
    node = graph._by_id[graph.opnode("getBalance")]
    assert node.verify is not None
    assert node.verify.status == "VERIFIED"


def test_from_docs_op_is_claimed_before_and_carries_verdict_after() -> None:
    # provenance is a separate axis: CLAIMED before verify, still CLAIMED after — with a verdict.
    assert verify.op_provenance(_DRAFT_SPEC, "getThing") == "CLAIMED"

    client = AgentApiClient(_DRAFT_SPEC, session=NoAuthSession())
    result = verify.verify_docs(client, mode="recorded")

    entry = result["report"]["getThing"]
    assert entry["provenance"] == "CLAIMED"  # provenance not overwritten
    assert entry["status"] == "UNVERIFIED"  # recorded -> no wire evidence -> honest
    node = client.surface_graph._by_id[client.surface_graph.opnode("getThing")]
    assert node.verify is not None


def test_extracted_op_keeps_provenance_and_gets_a_verdict() -> None:
    client = AgentApiClient(str(_FIXTURE), session=NoAuthSession())
    result = verify.verify_docs(client, mode="recorded")
    assert result["report"]["ping"]["provenance"] == "EXTRACTED"
    assert result["report"]["ping"]["status"] == "UNVERIFIED"


def test_cli_verify_docs_recorded_prints_json(capsys: Any) -> None:
    code = cli_main(["verify-docs", str(_FIXTURE)])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["mode"] == "recorded"
    assert payload["report"]["ping"]["status"] == "UNVERIFIED"
    assert payload["summary"] == {"verified": 0, "refuted": 0, "unverified": 1}
