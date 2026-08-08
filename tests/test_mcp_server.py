"""The question parameter: one concept, two published names.

`search_capabilities` advertises ``query``; `query_docs` and `find_start` advertise
``intent`` — the same thing, on the same surface. An agent that succeeded with one and
reused it got a validation error on its next call: a first-call failure in the product
whose whole claim is first-call-correct. Found by probing the hosted surfaces the way a
chat client would.
"""

from __future__ import annotations

from typing import Any


# --- the question parameter: one concept, two published names ----------------------


def _surface() -> Any:
    from gecko.client import AgentApiClient
    from gecko.ingest import load_spec
    from gecko.mcp_server import McpSurface

    return McpSurface(
        AgentApiClient(load_spec("gecko/examples/jupiter_swap_openapi.json"))
    )


def test_either_name_asks_the_same_question() -> None:
    """`search_capabilities` publishes `query`, `query_docs` and `find_start` publish
    `intent` — the same concept, on the same surface. An agent that succeeds with one
    and reuses it used to get a validation error on its next call. That is a first-call
    failure in the product whose whole claim is first-call-correct."""
    surface = _surface()

    for tool in ("search_capabilities", "query_docs"):
        by_query = surface.call_tool(tool, {"query": "swap tokens"})
        by_intent = surface.call_tool(tool, {"intent": "swap tokens"})

        assert by_query == by_intent, f"{tool} answers differently by parameter name"
        assert by_query, f"{tool} returned nothing"


def test_neither_name_is_mandatory_in_the_published_schema() -> None:
    """Keeping one of them `required` reintroduces the exact validation error for an
    agent that reached for the other."""
    tools = {t["name"]: t for t in _surface().list_tools()}

    for name in ("search_capabilities", "query_docs"):
        assert tools[name]["inputSchema"]["required"] == []


def test_both_names_are_advertised_so_an_agent_can_see_them() -> None:
    """Accepting an alias silently is not enough — the agent picks from the schema."""
    tools = {t["name"]: t for t in _surface().list_tools()}

    for name in ("search_capabilities", "query_docs"):
        properties = tools[name]["inputSchema"]["properties"]
        assert {"query", "intent"} <= set(properties)


def test_an_empty_question_still_reads_as_absent() -> None:
    """Whitespace under one name must not shadow a real question under the other."""
    from gecko.mcp_server import _question_of

    assert _question_of({"query": "   ", "intent": "real question"}) == "real question"
    assert _question_of({}) == ""


# --- the entry tool must not fail open ---------------------------------------------


def test_an_unknown_argument_name_is_named_not_ignored() -> None:
    """Making `query` optional so `intent` would work had a consequence nobody intended:
    EVERY name started "working". `search_capabilities(goal=...)` returned a successful,
    unranked dump of the whole catalog — top hits `root`, `live`, `ready` — with nothing
    telling the agent its argument had been ignored."""
    result = _surface().call_tool("search_capabilities", {"goal": "swap tokens"})

    assert isinstance(result, dict)
    assert "goal" in result["error"]
    assert "query" in result["error"]


def test_no_question_says_what_it_needs_instead_of_dumping_the_surface() -> None:
    """This is the ENTRY tool. An agent whose first call silently misfires has no reason
    to make a second one — which is what 47 enumerations and 0 calls looks like."""
    result = _surface().call_tool("search_capabilities", {})

    assert isinstance(result, dict)
    assert "query" in result["error"]


def test_both_advertised_names_still_work() -> None:
    """The fix must not undo the reason `required` was emptied in the first place.

    Asserts the ANSWER, not the container. This test originally read
    `assert not isinstance(by_query, dict)` — true when written, and broken hours later
    when scoped retrieval (#339) wrapped the result in a `{plan, tools}` envelope. The
    invariant was never "returns a list"; it is "either name reaches the same real
    answer, and neither is refused."
    """
    surface = _surface()

    by_query = surface.call_tool("search_capabilities", {"query": "swap tokens"})
    by_intent = surface.call_tool("search_capabilities", {"intent": "swap tokens"})

    assert by_query == by_intent
    assert "error" not in by_query
    assert by_query["tools"], (
        "a recognised question must return tools, not an empty scope"
    )


def test_query_docs_refuses_the_same_way() -> None:
    """Same resolver, same trap — it took the same gate."""
    result = _surface().call_tool("query_docs", {"topic": "swap"})

    assert isinstance(result, dict)
    assert "topic" in result["error"]
