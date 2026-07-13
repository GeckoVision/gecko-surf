"""The end-to-end story runs green, offline, $0 — the demo-day path can't silently break."""

from __future__ import annotations

from examples.txline_sharp_agent.story import run


def test_story_runs_end_to_end_returns_zero():
    assert run() == 0
