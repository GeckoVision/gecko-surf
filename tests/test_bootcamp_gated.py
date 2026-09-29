"""The class mounts need a student's Gecko key; the public doors stay open.

Founder ruling, 28 Sep 2026: this week's capstone runs through `/bootcamp/mcp`, with
each student on their own Gecko key (so every call is attributable and the Friday
mainnet purchases are tied to a person). Until this, `/bootcamp/mcp` answered 200 with
no key. `orquestra` stays the product's public front door, and `course` stays open so
asking the course a question needs no setup.
"""

from __future__ import annotations

import pytest

from gecko.serve_mcp import default_gated_surfaces


@pytest.fixture(autouse=True)
def _no_team_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_BOOTCAMP_TEAMS", raising=False)


def test_the_class_mount_is_gated_by_default() -> None:
    gated = default_gated_surfaces()
    assert "bootcamp" in gated
    assert "birdeye" in gated  # the paid surface stays gated


def test_every_team_mount_is_gated_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A team mount is the same surface as `bootcamp` under another name. Gating one
    and not the other would leave the class door open under a different URL."""
    monkeypatch.setenv("GECKO_BOOTCAMP_TEAMS", "alpha,beta")
    gated = default_gated_surfaces()
    assert {"bootcamp-alpha", "bootcamp-beta"} <= gated


def test_the_public_doors_stay_open() -> None:
    gated = default_gated_surfaces()
    assert "orquestra" not in gated
    assert "course" not in gated
