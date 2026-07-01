"""The Pegana demo reproduces the before/after + scorecard offline, $0, deterministic.

These tests are the falsifiable guard behind the founder-facing demo: they pin the
exact numbers Raff is shown (41 ops / 26 surfaced / 15 hidden), the 6/6 scorecard, the
mint-vs-symbol pick, and the JWT auth-boundary refusal — all from the committed fixture
with no network. If the engine or fixture drifts, the demo's claims fail here first.

Run: uv run pytest examples/pegana_demo/ -q
"""

from __future__ import annotations

import pytest

from examples.pegana_demo.demo import (
    JWT_GATED_OP,
    PEGANA_MCP_TOOLS,
    TASKS,
    build_report,
    main,
)


def test_surface_counts_are_the_verified_numbers() -> None:
    report = build_report()
    # The 41-vs-6 table's left/right sides — the exact figures shown to Raff.
    assert report.ops_total == 41
    assert report.surfaced == 26
    assert report.hidden == 15
    assert report.hidden == report.ops_total - report.surfaced
    assert report.mcp_tools == PEGANA_MCP_TOOLS == 6


def test_scorecard_is_all_first_call_correct() -> None:
    report = build_report()
    card = report.card
    assert card["top1_rate"] == 1.0
    assert card["top5_rate"] == 1.0
    assert card["well_formed_rate"] == 1.0
    assert len(card["results"]) == len(TASKS) == 6
    for r in card["results"]:
        assert r["top1"], f"{r['goal']} picked {r['picked']}, expected {r['expect']}"
        assert r["well_formed"], r["reason"]


def test_mint_vs_symbol_gotcha() -> None:
    # Holding a mint, the agent lands on the by-mint route, not the {symbol} route.
    report = build_report()
    assert report.mint_route == "/v1/assets/by-mint/{mint}/state"
    assert report.symbol_route == "/v1/assets/{symbol}/state"
    assert report.mint_route != report.symbol_route


def test_public_session_refuses_the_jwt_gated_op() -> None:
    # The auth boundary: a public read must never fire a /v1/me/* JWT op.
    report = build_report()
    assert report.jwt_refused
    assert JWT_GATED_OP not in {
        r["expect"] for r in report.card["results"]
    }  # never even proposed as a task target


def test_main_prints_offline_without_error(capsys: pytest.CaptureFixture[str]) -> None:
    # The founder runs `python examples/pegana_demo/demo.py`; it must render clean, $0.
    main()
    out = capsys.readouterr().out
    assert "Pegana: two ways an agent can reach it." in out
    assert "41 ops ingested" in out
    assert "top-1 100%" in out
    assert "correctly refused" in out
