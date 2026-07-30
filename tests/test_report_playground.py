"""Tests for the interactive Playground — the "watch an agent use your API" envelope.

The Playground is a self-contained, deterministic replay from the surface (no live model,
no network). These tests pin the interaction contract: clickable intent controls, one
panel per intent carrying the derived first-call-correct call, inline-only CSS/JS, a
JS-disabled fallback that is never blank, XSS-safe interpolation, and byte-stability.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gecko import report
from gecko.client import AgentApiClient
from gecko.ingest import load_spec

FIXTURE = str(Path(__file__).parent / "fixtures" / "pegana_openapi.json")
INTENTS = [
    "current peg state for a symbol",
    "list my alert subscriptions",
    "get peg state by mint address",
]


@pytest.fixture(scope="module")
def html() -> str:
    return report.build_scorecard(FIXTURE, intents=INTENTS)


def test_playground_renders_clickable_intent_buttons(html: str) -> None:
    # Real buttons (keyboard-accessible), one per intent, wired as a tablist.
    assert 'role="tablist"' in html
    chips = re.findall(r'<button[^>]*class="pg-chip"', html)
    assert len(chips) == len(INTENTS)
    # each chip is a real <button> acting as a tab controlling a panel
    assert 'role="tab"' in html
    assert "aria-controls=" in html


def test_playground_has_a_panel_per_intent_with_derived_call(html: str) -> None:
    panels = re.findall(r'role="tabpanel"', html)
    assert len(panels) == len(INTENTS)
    # the derived, first-call-correct calls for these intents (real endpoints)
    assert "derived tool" in html
    assert "/v1/assets/{symbol}/state" in html  # current peg state for a symbol
    assert "/v1/me/subs" in html  # list my alert subscriptions
    assert "/v1/assets/by-mint/{mint}/state" in html  # peg state by mint


def test_playground_shows_the_full_agent_moment(html: str) -> None:
    # provenance + safety posture make the trust-boundary value visible
    assert "prov" in html
    # the auth-required intent renders the "agent holds no key" posture
    assert "at call-time" in html
    assert "no key" in html.lower()


def test_playground_css_and_js_are_fully_inline() -> None:
    # Scoped to the playground fragment (the whole scorecard embeds an SVG whose XML
    # namespace is an http URL — not an external fetch).
    client = AgentApiClient(load_spec(FIXTURE))
    frag = report._render_playground(client, INTENTS)
    assert "<style" in frag and "<script" in frag
    # no external anything — self-contained single file
    assert "src=" not in frag
    assert "<link" not in frag
    assert "http://" not in frag and "https://" not in frag


def test_playground_escapes_interpolated_intent_text() -> None:
    evil = '<script>alert("xss")</script> peg state for a symbol'
    out = report.build_scorecard(FIXTURE, intents=[evil])
    assert "<script>alert" not in out  # never rendered as live markup
    assert "&lt;script&gt;" in out  # escaped instead


def test_playground_has_no_js_fallback(html: str) -> None:
    # JS disabled must never be a blank card: <noscript> reveals every panel,
    # and the first panel is open by default in the static HTML.
    assert "<noscript" in html
    assert "is-open" in html


def test_playground_is_deterministic() -> None:
    a = report.build_scorecard(FIXTURE, intents=INTENTS)
    b = report.build_scorecard(FIXTURE, intents=INTENTS)
    assert a == b
    # no wall-clock nondeterminism leaked into the generated HTML
    assert "Date.now" not in a


def test_playground_omitted_when_no_entries() -> None:
    # back-compat: empty intents derive nothing -> the section is omitted entirely.
    client = AgentApiClient(load_spec(FIXTURE))
    assert report._render_playground(client, []) == ""
