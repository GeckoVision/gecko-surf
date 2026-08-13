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
        # The MENU: store names and prices read from each store's own account — never an
        # authorization. Its own boundary is pinned in tests/test_store_directory.py.
        "list_stores",
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
    code = find_start_main(["swap sol for usdc", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["starts"][0]["program"] == "meteora"


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
    It can, and the discriminating pair is deliberately awkward: ``buy a house`` is an
    out-of-scope intent that CLEARS the floor and exits 0, while ``flumbuzzle the quantum
    wombat`` finds nothing and exits 1. So the code tracks "did anything clear the floor",
    which is not the same as "was the answer right" — ``buy a house`` exiting 0 is one of
    the 8 authored false accepts, not a success.

    Also pins that the CLI falsifies at PRODUCTION depth, checked behaviourally rather
    than by reading the argparse literal. ``exchange tokens at the best rate on meteora``
    is limit-sensitive: the router refuses it at 5 and serves a start at 10. Running it
    with no ``--limit`` must therefore exit 1 — the shallow answer. A falsifier read at a
    depth no agent is given would prove nothing about the agent's experience.
    """
    assert find_start_main(["buy a house"]) == 0
    capsys.readouterr()
    assert find_start_main(["flumbuzzle the quantum wombat"]) == 1
    capsys.readouterr()

    limit_sensitive = "exchange tokens at the best rate on meteora"
    assert find_start_main([limit_sensitive]) == 1, "the default must serve depth 5"
    capsys.readouterr()
    assert find_start_main([limit_sensitive, "--limit", "10"]) == 0
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
