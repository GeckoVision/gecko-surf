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

import json
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
        return (
            "Found 1 program(s):\n"
            f"- **jurassic_fi_token_sale** (projectId: `{PROJECT}`)\n"
            f"  Program: `{ADDRESS}`"
        )


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


def test_a_name_the_wired_index_misses_is_searched_in_the_catalog() -> None:
    """`jurassic_fi` is a bad SLUG and a perfectly good QUERY.

    This briefly refused the string outright, on the grounds that it could not name a
    project. That was better than the original `no_start` and still wrong: the catalog's
    own text search accepts free text and finds it, underscore and all. Refusing told an
    agent the name was unusable when the catalog would have answered it.

    The wired index gets first look; the catalog is the fallback; and the answer carries
    the program_id, which is the handle everything downstream actually takes.
    """
    catalog = FakeCatalog()
    result = OrquestraCatalogSurface(find_start_pages=0, catalog_mcp=catalog).call_tool(
        "find_start", {"intent": "buy into the token sale", "program": "jurassic_fi"}
    )

    assert result["catalog_matches"][0]["program_id"] == ADDRESS
    assert "program_ids" in result["hint"]
    assert catalog.asked[-1]["args"]["query"] == "jurassic_fi"


def test_an_address_costs_one_upstream_call_not_two() -> None:
    """An address resolves directly; it must not also trigger the name-search fallback.

    Worth pinning because both paths call the same upstream tool, and a stray second call
    would be invisible in the answer while doubling what we ask of a partner's service.
    """
    catalog = FakeCatalog()
    OrquestraCatalogSurface(find_start_pages=0, catalog_mcp=catalog).call_tool(
        "find_start", {"intent": "buy", "program": ADDRESS}
    )

    assert [c["args"] for c in catalog.asked] == [{"programId": ADDRESS}]


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


# --------------------------------------- pointers and misses, from the same session


class WithoutPrepare(OrquestraCatalogSurface):
    """A mount that does not offer `prepare_instruction` — a real possibility, and the
    shape the live agent's client presented (it was holding a stale tool list)."""

    def list_tools(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [t for t in super().list_tools() if t["name"] != "prepare_instruction"]


def test_find_start_never_points_at_a_tool_this_surface_does_not_offer() -> None:
    """A live agent followed this pointer and could not find the tool.

    Its client was holding a stale list, but the pointer had no business being
    unconditional either. `find_start` exists to tell an agent where to start; naming a
    tool that is not there is worse than naming none, because the agent either fails or
    starts hand-rolling the instruction — the exact thing this whole path prevents.
    """
    catalog = FakeCatalog()
    result = WithoutPrepare(find_start_pages=0, catalog_mcp=catalog).call_tool(
        "find_start", {"intent": "contribute", "program": ADDRESS}
    )

    assert result["next"]["tool"] != "prepare_instruction"
    assert result["next"]["tool"] in result["next"]["available_here"]
    # and it says plainly what the fallback cannot do, so nobody improvises the bytes
    assert "must not be hand-rolled" in result["next"]["why"]


def test_a_catalog_miss_is_an_answer_not_a_leaked_http_error() -> None:
    """`comprehend_program('deaton')` answered `GET https://…/api/deaton/pda failed:
    HTTP Error 404`. That leaks a URL and, worse, leaves an agent unable to tell "no such
    program" from "the catalog is down" — one is a fact about the world, the other is an
    outage.
    """

    class Missing(OrquestraCatalogSurface):
        def _catalog_client(self) -> Any:
            raise AssertionError("should not be reached")  # pragma: no cover

    surface = OrquestraCatalogSurface(find_start_pages=0)

    class Boom:
        def fetch_surface(self, project: str) -> Any:
            from gecko.orquestra_client import OrquestraClientError

            raise OrquestraClientError(
                f"GET https://api.orquestra.dev/api/{project}/pda failed: "
                "HTTP Error 404: Not Found"
            )

    surface.client = Boom()  # type: ignore[assignment]
    result = surface.call_tool("comprehend_program", {"project": "deaton"})

    assert result["not_found"] is True
    assert "error" not in result
    assert "api.orquestra.dev" not in json.dumps(result), (
        "no upstream URL in the answer"
    )
    assert "ADDRESS" in result["hint"]
