"""`start(intent)` — one call an agent can act on.

Measured before this existed: `find_start("buy a coffee")` returns **17,717 characters**,
of which the chosen start is 4,382. The other 13,335 are four runners-up, each carrying a
FULL derive plan — and three of the four are `guess`, which is the router's own word for
"below the floor, do not run this".

That is the shape of the complaint a live agent field report already made: it had to
assemble the tools itself, and comprehension did not persist between calls. An agent
reading this response pays for four plans to make one call.

The cut is not truncation, it is a rule: **a derive plan is a plan to CALL something, and
you do not plan a call you are not making.** The chosen start keeps everything it had.
Everything else becomes what a runner-up actually is — a name, a score, and why it lost.

Additive by construction: this projects a `FindStartResult` and changes neither
`find_start` nor `StartPoint`, so nothing that reads the full shape is affected.
"""

from __future__ import annotations

import json

from gecko.find_start import find_start
from gecko.start_view import start_view


def test_the_chosen_start_keeps_everything_it_had() -> None:
    """The cut must not reach the thing the agent is about to run."""
    result = find_start("buy a coffee", limit=5)
    view = start_view(result)

    full = next(p for p in result.to_json()["starts"] if p["kind"] == "start")
    assert view["start"] == full
    assert view["start"]["derive_plan"]
    assert view["start"]["chain"]


def test_a_runner_up_carries_no_derive_plan() -> None:
    """You do not plan a call you are not making."""
    view = start_view(find_start("buy a coffee", limit=5))

    assert view["alternatives"]
    for alt in view["alternatives"]:
        assert "derive_plan" not in alt
        assert "chain" not in alt
        assert "preludes" not in alt


def test_a_runner_up_still_says_what_it_is_and_why_it_lost() -> None:
    """Dropping the plan must not drop the evidence. An agent that disagrees with the
    ranking needs enough to ask for the runner-up by name."""
    view = start_view(find_start("buy a coffee", limit=5))
    alt = view["alternatives"][0]

    assert alt["program"]
    assert "kind" in alt
    assert "score" in alt
    assert "why" in alt


def test_a_refusal_survives_the_projection() -> None:
    """The most important thing the router says is sometimes "no". A view that quietly
    presented the top guess as a start would undo the floor we just built."""
    result = find_start("buy a house", limit=5)
    view = start_view(result)

    assert result.no_start
    assert view["no_start"] is True
    assert view["start"] is None
    assert view["note"] == result.note
    assert view["alternatives"], "the closest candidates are still offered, as guesses"


def test_start_is_present_exactly_when_the_router_found_one() -> None:
    for intent in (
        "buy a coffee",
        "contribute to a launch",
        "buy a house",
        "flumbuzzle",
    ):
        result = find_start(intent, limit=5)
        view = start_view(result)
        assert (view["start"] is None) is result.no_start, intent


def test_the_response_an_agent_reads_is_a_fraction_of_the_old_one() -> None:
    """The number this exists for. Not a fixed byte budget — a ratio against the full
    shape, so the assertion keeps meaning something as the surface grows."""
    for intent in ("buy a coffee", "contribute to a launch"):
        result = find_start(intent, limit=5)
        before = len(json.dumps(result.to_json()))
        after = len(json.dumps(start_view(result)))

        assert after < before / 2, (
            f"{intent!r}: {after:,} is not less than half of {before:,}"
        )


def test_nothing_in_the_view_was_invented() -> None:
    """Every field is lifted from the result. A projection that synthesizes a field is a
    second source of truth about what the router decided."""
    result = find_start("buy a coffee", limit=5)
    raw = result.to_json()
    view = start_view(result)

    by_key = {(p["program"], p["instruction"]): p for p in raw["starts"]}
    for alt in view["alternatives"]:
        source = by_key[(alt["program"], alt["instruction"])]
        for key, value in alt.items():
            assert source[key] == value, key


def test_the_tool_reaches_the_agent_and_agrees_with_find_start() -> None:
    """Wired is not the same as reaches-the-agent, so this goes through the real MCP
    surface. The two tools must never disagree about what the router decided — `start`
    reuses the whole `find_start` handler rather than re-deriving anything, and this is
    what holds that."""
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    surface = OrquestraCatalogSurface(find_start_pages=0)
    assert "start" in [tool["name"] for tool in surface.list_tools()]

    for intent in ("buy a coffee", "contribute to a launch", "buy a house"):
        full = surface.call_tool("find_start", {"intent": intent})
        view = surface.call_tool("start", {"intent": intent})

        assert view["no_start"] == full["no_start"], intent
        assert view["note"] == full["note"], intent
        runnable = [p for p in full["starts"] if p["kind"] == "start"]
        assert view["start"] == (runnable[0] if runnable else None), intent


def test_an_error_passes_through_unprojected() -> None:
    """A missing intent is an error dict, not a result. Projecting it would invent a
    `start: null` and report a refusal the router never made."""
    from gecko.providers.catalog_surface import OrquestraCatalogSurface

    out = OrquestraCatalogSurface(find_start_pages=0).call_tool(
        "start", {"intent": " "}
    )

    assert "error" in out
    assert "start" not in out
