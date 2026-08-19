"""The Orquestra catalog surface (MCP) + the ``find-start`` CLI — offline.

The surface is duck-typed (list_tools/call_tool) like every program surface;
every network path goes through the injected http seam against the saved
fixtures (real captured catalog responses — public program metadata only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.providers.cli import PROGRAMS
from gecko.orquestra_client import OrquestraClient
from gecko.providers.catalog_surface import OrquestraCatalogSurface
from gecko.providers.cli import find_start_main, main

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
BASE = "https://api.orquestra.dev/api"
PUMP_SLUG = "6i6q26bmm46b89xlxo1kv"


def _client(extra: dict[str, bytes] | None = None) -> OrquestraClient:
    responses = {f"{BASE}/projects?page=1": (FIXTURES / "projects.json").read_bytes()}
    responses.update(extra or {})

    def http_get(url: str) -> bytes:
        if url not in responses:
            raise AssertionError(f"unexpected GET {url}")
        return responses[url]

    return OrquestraClient(base_url=BASE, http_get=http_get)


def _surface(extra: dict[str, bytes] | None = None) -> OrquestraCatalogSurface:
    return OrquestraCatalogSurface(client=_client(extra))


# --- duck-typed MCP surface -----------------------------------------------------


def test_surface_lists_the_catalog_tools() -> None:
    names = [t["name"] for t in _surface().list_tools()]
    assert names == [
        "find_start",
        "list_programs",
        "comprehend_program",
        # The pre-flight: it plans and verifies, and hands back UNSIGNED bytes. Its own
        # boundary is pinned in tests/test_prepare_purchase_tool.py.
        "prepare_purchase",
        # Its rehearsal twin, deliberately adjacent: the same purchase driven all the way
        # through on a LOCAL fork. The only tool here that signs, and the only one that
        # can say what actually moved — pinned in tests/test_try_purchase_tool.py.
        "try_purchase",
        # The MENU: store names and prices read from each store's own account — never an
        # authorization. Its own boundary is pinned in tests/test_store_directory.py.
        "list_stores",
        # The OTHER half of the handoff: prepare_purchase hands out bytes and a binding,
        # somebody else signs, and this proves the signed bytes are the checked ones
        # before broadcast. Keyless like the rest — pinned in tests/test_verify_signed.py.
        "verify_signed_transaction",
        # The GENERAL form of prepare_purchase: any instruction of any catalog program,
        # PDAs derived here and bytes built by the catalog's own builder. Unsigned like
        # the rest — pinned in tests/test_prepare_instruction.py.
        "prepare_instruction",
        # Derivation as a primitive — pinned here because a correct refusal that leaves
        # the caller to hand-roll a bump loop pushes the guess outside where we can
        # check it, which is how a valid-looking wrong address gets built.
        "derive_ata",
        "derive_pda",
        # The edges. A catalogue lists instructions independently and a list has no
        # edges, so "claim must follow contribute" is a fact only a graph can state.
        "program_lifecycle",
    ]


def test_find_start_tool_routes_the_pump_intent() -> None:
    out = _surface().call_tool(
        "find_start", {"intent": "buy this token on pump and hold it"}
    )
    assert out["no_start"] is False
    top = out["starts"][0]
    assert (top["program"], top["instruction"]) == ("pumpfun", "buy")
    # provenance tags survive the JSON boundary — the demo payload is honest
    plans = {s["account"]: s["provenance"] for s in top["derive_plan"]}
    assert plans["bonding_curve_v2"] == "recovered"
    assert plans["fee_recipient"] == "flagged"


def test_find_start_tool_degrades_honestly_when_the_catalog_is_down() -> None:
    def failing_get(url: str) -> bytes:
        raise OSError("connection refused")

    surface = OrquestraCatalogSurface(
        client=OrquestraClient(base_url=BASE, http_get=failing_get)
    )
    out = surface.call_tool("find_start", {"intent": "swap sol for usdc"})
    assert out["starts"][0]["program"] == "meteora"  # the wired index still answers
    assert "catalog unavailable" in out["catalog_note"]


def test_find_start_tool_requires_an_intent() -> None:
    assert "error" in _surface().call_tool("find_start", {})


def test_list_programs_returns_wired_plus_the_paginated_catalog() -> None:
    out = _surface().call_tool("list_programs", {"page": 1})
    wired = {w["program"] for w in out["wired"]}
    # Kept in lockstep with the registry so wiring a program without listing it
    # here fails loudly rather than drifting silently.
    assert wired == set(PROGRAMS)
    assert out["catalog"]["page"] == 1
    assert out["catalog"]["total_pages"] == 225  # the real catalog is paginated
    assert out["catalog"]["total"] == 4500
    assert len(out["catalog"]["projects"]) == 20
    assert "comprehend_program" in out["catalog"]["note"]


def test_comprehend_program_generates_a_config_from_fixtures() -> None:
    surface = _surface(
        {
            f"{BASE}/{PUMP_SLUG}/pda": (FIXTURES / PUMP_SLUG / "pda.json").read_bytes(),
            f"{BASE}/{PUMP_SLUG}/idl": (FIXTURES / PUMP_SLUG / "idl.json").read_bytes(),
        }
    )
    out = surface.call_tool("comprehend_program", {"project": PUMP_SLUG})
    assert out["config"]["kind"] == "program"
    assert (
        out["config"]["program"]["program_id"]
        == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    )
    # honesty: per-PDA provenance rides along; surface-only run is labeled as such
    assert all("tier" in p for p in out["provenance"].values())
    assert "no manual overlay" in out["note"]


def test_unknown_tool_is_an_error() -> None:
    assert "error" in _surface().call_tool("nope", {})


# --- CLI ------------------------------------------------------------------------


def test_cli_find_start_prints_the_ranked_start_legibly(capsys) -> None:
    code = find_start_main(["buy this token on pump and hold it"])
    out = capsys.readouterr().out
    assert code == 0
    assert "START 1. pumpfun/buy" in out
    assert "[recovered]" in out  # provenance tags visible
    assert "[FLAGGED]" in out  # flagged gaps visible
    assert "plan_buy" in out


def test_cli_find_start_json_round_trips(capsys) -> None:
    # Was "swap sol for usdc" until 2026-08-19; that intent is now a documented casualty
    # of the identity-term gate (meteora's card describes its implementation, not the
    # task). This test is about JSON round-tripping, so it uses an intent that still
    # routes rather than pinning the regression twice.
    code = find_start_main(["stake my tokens on ore", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["starts"][0]["program"] == "ore"


def test_cli_find_start_no_start_exits_1_and_says_so(capsys) -> None:
    code = find_start_main(["flumbuzzle the quantum wombat"])
    out = capsys.readouterr().out
    assert code == 1
    assert "NO START FOUND" in out
    assert "GUESS" in out


def test_the_cli_exit_code_is_a_two_sided_falsifier_at_the_production_limit(
    capsys,
) -> None:
    """R-4. PR-7's DoD was unsatisfiable as written; THIS is the falsifier it has.

    The exit code is only evidence if it can come out both ways for the reason claimed.

    The pair used to be deliberately awkward: ``buy a house`` was an OUT-OF-SCOPE intent
    that cleared the floor and exited 0, and this docstring called that "one of the 8
    authored false accepts, not a success". On 2026-08-19 the identity-term gate closed
    it — ``buy a house`` now exits 1 — so the 0 side moves to an in-scope intent and the
    pair stops being awkward. Recorded rather than quietly swapped: the old pair was
    documenting a defect, and the defect is gone.

    The production-depth half went with it. It rested on ``exchange tokens at the best
    rate on meteora`` being limit-sensitive — refused at 5, served at 10. No row in the
    golden set is limit-sensitive any more (checked directly, and
    ``test_the_router_refuses_at_serve_limit_what_it_serves_at_eval_limit`` records the
    same closure from the other side), so the property cannot be pinned behaviourally
    today. It is asserted here as an invariant instead: a row refused at the default depth
    must stay refused when the caller looks deeper, because a floor that depends on how
    hard you look is not a floor.
    """
    assert find_start_main(["buy a beer"]) == 0
    capsys.readouterr()
    assert find_start_main(["flumbuzzle the quantum wombat"]) == 1
    capsys.readouterr()
    # the former false accept, now the second falsifying side
    assert find_start_main(["buy a house"]) == 1
    capsys.readouterr()

    # depth must not rescue a refusal
    assert find_start_main(["buy a house", "--limit", "10"]) == 1
    capsys.readouterr()


def test_cli_find_start_log_misses_is_opt_in_and_categorical(
    tmp_path: Path, capsys
) -> None:
    log = tmp_path / "misses.jsonl"
    find_start_main(["flumbuzzle the quantum wombat", "--log-misses", str(log)])
    capsys.readouterr()
    record = json.loads(log.read_text(encoding="utf-8").strip())
    # v1 counts kept; v2 adds candidate NAMES/scores + floor (still categorical)
    assert {
        "intent_term_count",
        "matched_score",
        "wired_program_count",
        "top_candidates",
        "margin",
        "floor",
    } <= set(record)
    assert record["floor"] == "guess"
    assert "wombat" not in log.read_text(encoding="utf-8")


def test_gecko_orquestra_dispatches_find_start(capsys) -> None:
    code = main(["find-start", "buy this token on pump and hold it"])
    assert code == 0
    assert "pumpfun/buy" in capsys.readouterr().out


def test_catalog_serve_flag_is_a_valid_target() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--catalog", "--help"])
    assert exc.value.code == 0


def test_program_and_catalog_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--program", "meteora", "--catalog"])
    assert exc.value.code == 2


def test_the_surface_tells_a_client_how_its_tools_fit_together() -> None:
    """MCP's `instructions` is read ONCE, before any tool is chosen — the only place a
    rule about the ORDER of tools can live.

    A per-tool description cannot carry it: by the time an agent reads `prepare_purchase`,
    it has already decided to call it. A real session routed perfectly and still took the
    worse path, because nothing told it beforehand how the pieces fit.
    """
    instructions = OrquestraCatalogSurface.instructions

    # The order, and the two steps agents skipped in practice.
    assert "BROWSE" in instructions and "list_stores" in instructions
    assert "GET A SIGNER before you need one" in instructions
    assert "api.paybox.sh/mcp" in instructions
    assert "FUND" in instructions
    assert "verify_signed_transaction" in instructions
    # The boundary, stated once where it cannot be missed — and it has to stay TRUE now
    # that one tool here does sign: the claim is about the caller's money, and the
    # exception is named in the same breath rather than left for a tool description.
    assert "Nothing here holds a key to anybody's money" in instructions
    assert "The one tool that signs, `try_purchase`" in instructions
    # When to reach for the rehearsal instead of the real thing, and what it costs.
    assert "REHEARSE INSTEAD WHEN NOBODY WANTS THE PRODUCT YET" in instructions
    assert "a rehearsal buys nobody a coffee" in instructions
    assert "surfpool start" in instructions
    # And the rule that keeps a refusal from being treated as a bug to work around.
    assert "A REFUSAL IS AN ANSWER" in instructions


def test_the_served_app_hands_those_instructions_to_the_client() -> None:
    """Carrying the text is not the same as SENDING it.

    The first version of this test built its own `Server` and asserted the value it had
    just passed in — it measured itself, and stayed green when the app stopped forwarding
    anything. This drives the real app and reads what a client would actually receive.
    """
    import json

    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    app = build_multi_surface_app(
        [("orquestra", OrquestraCatalogSurface())], allowed_hosts=["testserver"]
    )
    with TestClient(app) as client:
        response = client.post(
            "/orquestra/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    body = next(
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    )
    served = body["result"].get("instructions")

    assert served, "the client is told nothing about how to use this surface"
    assert "GET A SIGNER before you need one" in served
    assert "A REFUSAL IS AN ANSWER" in served


def test_a_surface_without_instructions_still_serves() -> None:
    """Opt-in: anything that carries no `instructions` sends None, exactly as before."""
    from gecko.http_server import build_http_app

    class _Bare:
        def list_tools(self, **_kw):
            return []

        def call_tool(self, name, arguments):
            return {}

    assert build_http_app(_Bare(), server_name="bare") is not None
