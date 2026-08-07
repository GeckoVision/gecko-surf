"""verify-docs must not defame a healthy API: REFUTED needs evidence of ABSENCE.

The bug this pins (measured on Pegana, a design partner whose API grades A 100/100 on our
own scorecard: verified 13 / **refuted 12** / unverified 3, with all five hand-spot-checked
ops returning 200 with REAL arguments): ``verify-docs --live`` synthesizes an argument for
every required path/query param. When the synthesized value is an invented placeholder
(``"sample"``, ``0``) the API correctly answers 404 — and we published that as REFUTED,
i.e. "this documented endpoint does not exist". Two different facts collapsed into one
verdict:

    "the endpoint is not there"   vs   "my made-up argument is not a real entity"

The rule these tests pin:

* REFUTED requires an ABSENCE status (404/405/410) **and** no synthesized route argument.
* A synthesized route argument turns any error status into UNVERIFIED, and the basis says
  which param we could not ground (``no-real-argument:<param>``).
* A status that proves the endpoint ANSWERED (400/401/403/422/429/5xx) never refutes — the
  server routed the request and then complained about it.
* The genuine signal survives: a fabricated endpoint with no synthesized route argument is
  still REFUTED (the flagship docs-fabrication demo).

Pattern B throughout: one injected fake transport per test, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gecko import verify
from gecko.access import NoAuthSession, public_session
from gecko.caller import PreparedRequest
from gecko.canonical import USDC_MINT
from gecko.client import AgentApiClient
from gecko.sample import example_from_schema, example_is_grounded
from gecko.validator import synthesized_route_args

_PEGANA = Path(__file__).parent / "fixtures" / "pegana_p0_openapi.json"


def _spec(paths: dict[str, Any]) -> dict[str, Any]:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Widget API", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": paths,
    }


_OK_RESPONSE = {
    "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
}


def _get(op_id: str, params: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    op: dict[str, Any] = {
        "operationId": op_id,
        "summary": f"Read {op_id}.",
        "responses": _OK_RESPONSE,
    }
    if params:
        op["parameters"] = params
    return {"get": op}


def _client(spec: dict[str, Any], status: int, body: Any = None) -> AgentApiClient:
    """A surface whose whole upstream answers one fixed status — the light fake."""

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        return status, body if body is not None else {"error": "x"}

    return AgentApiClient(
        spec,
        base_url="https://api.example.com",
        session=NoAuthSession(),  # nothing to inject -> live never degrades
        live_transport=transport,
    )


# --- the reproduction: a synthesized path arg must never earn REFUTED -------------------
def test_synthesized_path_arg_404_is_unverified_not_refuted() -> None:
    """THE BUG. ``/widgets/{id}`` ships no example, so we invent ``id="sample"``; the API
    correctly 404s a widget that does not exist. That is not evidence the ENDPOINT is
    missing — the verdict must be UNVERIFIED and say which param we could not ground."""
    spec = _spec(
        {
            "/widgets/{id}": _get(
                "getWidget",
                [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            )
        }
    )

    result = verify.verify_docs(_client(spec, 404), mode="live")

    entry = result["report"]["getWidget"]
    assert entry["status"] == "UNVERIFIED"
    assert entry["basis"] == ["replay:404", "no-real-argument:id"]
    assert result["summary"] == {"verified": 0, "refuted": 0, "unverified": 1}


def test_synthesized_query_arg_400_is_unverified_not_refuted() -> None:
    """The Pegana ``/v1/audit.csv`` shape: required query params we invent values for, and
    a 400 back. A 400 means the server ROUTED the call and disliked our arguments."""
    spec = _spec(
        {
            "/audit.csv": _get(
                "auditCsv",
                [
                    {
                        "name": "from",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "format": "date-time"},
                    }
                ],
            )
        }
    )

    entry = verify.verify_docs(_client(spec, 400), mode="live")["report"]["auditCsv"]

    assert entry["status"] == "UNVERIFIED"
    assert entry["basis"] == ["replay:400", "no-real-argument:from"]


# --- statuses that prove the endpoint ANSWERED never refute -----------------------------
def test_error_status_on_a_param_free_op_never_refutes_when_it_proves_existence() -> (
    None
):
    """The Pegana ``/v1/ws`` shape: no params at all, but a 400 (a websocket route
    rejecting a plain GET). The server answered — that endpoint exists."""
    spec = _spec({"/ws": _get("wsHandler")})

    entry = verify.verify_docs(_client(spec, 400), mode="live")["report"]["wsHandler"]

    assert entry["status"] == "UNVERIFIED"
    assert entry["basis"] == ["replay:400", "no-evidence:endpoint-answered"]


def test_401_is_not_a_refutation() -> None:
    """A 401 is the strongest possible proof the endpoint EXISTS: the API routed the
    request and demanded credentials for it."""
    spec = _spec({"/private": _get("getPrivate")})

    entry = verify.verify_docs(_client(spec, 401), mode="live")["report"]["getPrivate"]

    assert entry["status"] == "UNVERIFIED"


def test_500_is_not_a_refutation() -> None:
    spec = _spec({"/flaky": _get("getFlaky")})

    entry = verify.verify_docs(_client(spec, 500), mode="live")["report"]["getFlaky"]

    assert entry["status"] == "UNVERIFIED"


# --- the genuine signal survives --------------------------------------------------------
def test_param_free_404_is_still_refuted() -> None:
    """The flagship docs-fabrication case: nothing was synthesized, the path itself is not
    served. This is the one shape that still earns the red badge."""
    spec = _spec({"/ghost": _get("getGhost")})

    entry = verify.verify_docs(_client(spec, 404), mode="live")["report"]["getGhost"]

    assert entry["status"] == "REFUTED"
    assert entry["basis"] == ["replay:404"]


def test_405_on_a_param_free_op_refutes_the_documented_method() -> None:
    spec = _spec({"/things": _get("listThings")})

    entry = verify.verify_docs(_client(spec, 405), mode="live")["report"]["listThings"]

    assert entry["status"] == "REFUTED"


def test_grounded_path_arg_404_still_refutes() -> None:
    """When the SPEC ships the example, the argument is the provider's own claim — a 404
    on it is evidence-backed and attributable, so the refutation stands."""
    spec = _spec(
        {
            "/widgets/{id}": _get(
                "getWidget",
                [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "example": "wid_live_1",
                        "schema": {"type": "string"},
                    }
                ],
            )
        }
    )

    entry = verify.verify_docs(_client(spec, 404), mode="live")["report"]["getWidget"]

    assert entry["status"] == "REFUTED"


# --- the grounding predicate mirrors the synthesizer ------------------------------------
def test_example_is_grounded_mirrors_example_from_schema() -> None:
    grounded: list[dict[str, Any]] = [
        {"type": "string", "example": "abc"},
        {"type": "string", "default": "abc"},
        {"type": "string", "enum": ["abc"]},
        {"type": "string", "x-gecko-entity": "solana-token-mint"},
    ]
    invented: list[dict[str, Any]] = [
        {"type": "string"},
        {"type": "string", "format": "date-time"},
        {"type": "integer"},
        # DECLARED but with no registered canonical -> still a placeholder.
        {"type": "string", "x-gecko-entity": "solana-pool"},
    ]
    for schema in grounded:
        assert example_is_grounded(schema) is True, schema
    for schema in invented:
        assert example_is_grounded(schema) is False, schema
        # the mirror: what the synthesizer actually fills is one of OUR placeholders.
        assert example_from_schema(schema) in ("sample", 0, "2026-06-26T00:00:00Z")


def test_synthesized_route_args_names_params_never_values() -> None:
    """Control plane: the gate reports param NAMES only, and ignores header/body args
    (a synthesized header cannot be why the API said "no such resource")."""
    spec = _spec(
        {
            "/widgets/{id}": _get(
                "getWidget",
                [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "cursor",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "example": "c0"},
                    },
                    {
                        "name": "X-Trace",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                ],
            )
        }
    )
    client = _client(spec, 200)

    assert synthesized_route_args(client._tool_by_name["getWidget"]) == ("id",)


# --- end to end on the real partner surface (offline fixture, injected transport) --------
def _pegana_transport(req: PreparedRequest) -> tuple[int, Any]:
    """How the real Pegana behaves: 200 for a real entity, 404 for a made-up one, and a
    400 for the websocket route + the invented audit window."""
    if "sample" in req.url or "audit.csv" in req.url:
        return (400 if "audit.csv" in req.url or "/ws" in req.url else 404), {"e": 1}
    if req.url.endswith("/v1/ws"):
        return 400, {"e": 1}
    return 200, {"ok": True}


def test_pegana_surface_is_never_falsely_refuted() -> None:
    """The measured regression, offline: not one op on a healthy partner API may come back
    REFUTED because WE could not supply a real argument."""
    client = AgentApiClient(
        str(_PEGANA),
        base_url="https://api.pegana.xyz",
        session=public_session(),
        live_transport=_pegana_transport,
    )

    result = verify.verify_docs(client, mode="live")

    assert result["summary"]["refuted"] == 0
    for op_id, entry in result["report"].items():
        assert entry["status"] != "REFUTED", op_id


def test_pegana_by_mint_verifies_once_the_mint_domain_is_declared() -> None:
    """The other half of the fix: a DECLARED value domain grounds the argument, so the op
    goes from "could not verify" to a real, wire-backed VERIFIED."""

    def transport(req: PreparedRequest) -> tuple[int, Any]:
        return (200, {"ok": True}) if USDC_MINT in req.url else (404, {"e": 1})

    client = AgentApiClient(
        str(_PEGANA),
        base_url="https://api.pegana.xyz",
        session=public_session(),
        declared_hints={"mint": "solana-token-mint"},
        live_transport=transport,
    )

    result = verify.verify_docs(client, mode="live")

    for op_id in ("detail_by_mint", "state_by_mint", "peg_feed_by_mint"):
        assert result["report"][op_id]["status"] == "VERIFIED"
