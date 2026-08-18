"""What a caller may pass as `program`, and why the three answers must differ.

FROM A LIVE AGENT SESSION. Asked for `jurassic_fi`, `comprehend_program` refused it plainly
("invalid project slug, alnum/dash only") while `find_start` accepted the same string and
answered `no_start` — the ordinary shape for a genuine miss. The agent concluded the program
did not exist, when the truth was that the string could never name one. It then tried
`jurassic-fi`, `jurassic`, and two catalog pages of 226 before giving up and asking for the
program address.

Every test here exists so that session ends differently.
"""

from __future__ import annotations

from typing import Any

import pytest

from gecko.providers.catalog_surface import (
    OrquestraCatalogSurface,
    classify_program_hint,
)

ADDRESS = "raWrRH5R3Ym7rRFry3T8YrED6nBcUUVN2HLAdmtQLdm"
PROJECT = "d2decec3-acdf-4946-bbb7-252a3c14ce2c"


class FakeCatalog:
    """A catalog MCP that answers `search_programs` and records what it was asked."""

    def __init__(self, *, found: bool = True) -> None:
        self.found = found
        self.asked: list[dict[str, Any]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.asked.append({"tool": name, "args": arguments})
        if not self.found:
            return "No programs matched."
        return f"Found 1 program(s):\n- **jurassic_fi_token_sale** (projectId: `{PROJECT}`)"


@pytest.mark.parametrize(
    "hint,expected",
    [
        ("jurassic_fi", "impossible"),  # the underscore the live session tried
        ("jurassic fi", "impossible"),  # a human name, with a space
        ("jurassic-fi", "slug"),  # shaped like a slug, may still not exist
        ("6i6q26bmm46b89xlxo1kv", "slug"),  # a real opaque catalog slug
        (ADDRESS, "address"),  # the handle the chain actually gives you
    ],
)
def test_a_hint_is_classified_before_it_is_searched(hint: str, expected: str) -> None:
    assert classify_program_hint(hint) == expected


def test_an_impossible_string_is_refused_rather_than_reported_missing() -> None:
    """`no_start` means "nothing matched your intent". Using it here tells an agent the
    program does not exist, which is a different and wrong claim."""
    result = OrquestraCatalogSurface(find_start_pages=0).call_tool(
        "find_start", {"intent": "contribute to the launch", "program": "jurassic_fi"}
    )

    assert "no_start" not in result
    assert "invalid project slug 'jurassic_fi'" in result["error"]
    # a refusal an agent cannot act on is a shrug — this one names the way out
    assert "ADDRESS" in result["hint"]


def test_an_address_resolves_and_points_at_the_tool_that_takes_one() -> None:
    catalog = FakeCatalog()
    result = OrquestraCatalogSurface(find_start_pages=0, catalog_mcp=catalog).call_tool(
        "find_start", {"intent": "contribute to the launch", "program": ADDRESS}
    )

    assert result["program_id"] == ADDRESS
    assert result["project_id"] == PROJECT
    assert result["next"]["tool"] == "prepare_instruction"
    assert catalog.asked[0]["args"] == {"programId": ADDRESS}


def test_an_address_the_catalog_does_not_index_is_an_ordinary_miss() -> None:
    """Here `no_start` IS right: the string was a valid handle and nothing was found."""
    catalog = FakeCatalog(found=False)
    result = OrquestraCatalogSurface(find_start_pages=0, catalog_mcp=catalog).call_tool(
        "find_start", {"intent": "anything", "program": ADDRESS}
    )

    assert result["no_start"] is True
    assert result["program_id"] == ADDRESS
    assert "does not index" in result["reason"]


def test_a_slug_shaped_hint_still_takes_the_ordinary_path() -> None:
    """Shaped like a slug is not the same as existing — this must stay a normal miss,
    or the new refusal would swallow every real not-found."""
    result = OrquestraCatalogSurface(find_start_pages=0).call_tool(
        "find_start",
        {"intent": "do something nobody offers", "program": "not-a-real-slug"},
    )

    assert result.get("no_start") is True
    assert "error" not in result
