"""The course surface: what it answers, what it refuses, and what it will not serve.

Every test here builds its own corpus in a tmp dir. Nothing reads the real course,
so these run offline and say the same thing on a machine that has never seen it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gecko.doccorpus import DocIndex, load_pages
from gecko.providers.course_surface import (
    MIN_COVERAGE,
    CourseSurface,
    build_course_surface,
)

PAGES = {
    "units/en/unit1/loops.mdx": (
        "# Loops and graphs\n\n"
        "A loop repeats one step until a budget runs out. A graph declares which "
        "transitions are legal before anything runs.\n"
    ),
    "units/en/unit1/retrieval.mdx": (
        "# Retrieval baselines\n\n"
        "Keyword search scores a passage by the words it shares with the question.\n"
    ),
    "units/en/unit1/quiz.mdx": "# Quiz\n\nQ1. What is a loop? Answer: it repeats.\n",
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    for rel, text in PAGES.items():
        page = tmp_path / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def surface(corpus: Path) -> CourseSurface:
    built = build_course_surface(corpus)
    assert built is not None
    return built


def test_it_finds_the_page_that_answers(surface: CourseSurface) -> None:
    result = surface.call_tool("search_course", {"query": "what is a graph transition"})
    assert result["hits"], "a question the corpus answers returned nothing"
    assert result["hits"][0]["page_id"] == "units/en/unit1/loops"


def test_an_out_of_scope_question_is_refused(surface: CourseSurface) -> None:
    """The finding this surface was fixed for.

    Measured 2026-09-24 against the real course: the out-of-scope pass rate was 23%
    while the module docstring said it refused by construction. "how do I bake
    sourdough bread at home" returned a page because one term, `home`, appears in
    course prose. One brushed content word out of four is not evidence.
    """
    result = surface.call_tool(
        "search_course", {"query": "how do I bake sourdough bread at home"}
    )
    assert result["hits"] == []
    assert "may not cover this" in result["note"]


def test_the_refusal_is_said_in_the_payload_not_only_the_description(
    surface: CourseSurface,
) -> None:
    """A model that skipped the tool description still reads what it got back."""
    search = next(t for t in surface.list_tools() if t["name"] == "search_course")
    assert "EMPTY RESULT IS AN ANSWER" in search["description"]
    refused = surface.call_tool("search_course", {"query": "sourdough bread recipe"})
    assert refused["hits"] == []
    assert refused["note"]


def test_a_quiz_is_never_retrievable(surface: CourseSurface) -> None:
    """A quiz is an answer key, and an answer key that is retrievable is retrieved."""
    assert not [page for page in surface.index.pages if "quiz" in page]
    listed = surface.call_tool("list_course_pages", {})
    assert not [row for row in listed["pages"] if "quiz" in row["page_id"]]


def test_reading_a_page_by_id(surface: CourseSurface) -> None:
    result = surface.call_tool("read_course_page", {"page_id": "units/en/unit1/loops"})
    assert result["found"] is True
    assert "transitions are legal" in result["text"]


def test_an_unknown_page_id_is_refused_with_something_to_do(
    surface: CourseSurface,
) -> None:
    result = surface.call_tool("read_course_page", {"page_id": "units/en/unit1/nope"})
    assert result["found"] is False
    assert "list_course_pages" in result["note"]


def test_listing_filters_by_prefix(surface: CourseSurface) -> None:
    assert surface.call_tool("list_course_pages", {"prefix": "units/en/unit1"})["count"] == 2
    assert surface.call_tool("list_course_pages", {"prefix": "units/en/unit9"})["count"] == 0


def test_an_empty_corpus_does_not_mount(tmp_path: Path) -> None:
    """The one that stops a broken deploy from looking like a correct refusal."""
    assert build_course_surface(tmp_path) is None


def test_a_missing_root_does_not_mount(tmp_path: Path) -> None:
    assert build_course_surface(tmp_path / "does-not-exist") is None


def test_no_tool_takes_a_path_a_url_or_text(surface: CourseSurface) -> None:
    """Control plane. Nothing a caller says can make this read a file or fetch."""
    for tool in surface.list_tools():
        properties = tool["inputSchema"].get("properties", {})
        assert not {"path", "url", "root", "text", "file"} & set(properties), tool[
            "name"
        ]
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is True


def test_the_text_files_carry_every_page_and_no_quiz(surface: CourseSurface) -> None:
    artifacts = surface.artifacts()
    assert set(artifacts) == {"llms.txt", "llms-full.txt"}
    full = artifacts["llms-full.txt"]
    for page_id in surface.index.pages:
        assert page_id in full
    assert "Answer: it repeats" not in full, "a quiz leaked into the one-fetch file"


def test_llms_txt_points_at_the_full_file(surface: CourseSurface) -> None:
    """The whole point for a client that cannot speak MCP."""
    assert "llms-full.txt" in surface.artifacts()["llms.txt"]


def test_the_coverage_floor_is_the_measured_one() -> None:
    """0.5 is not a taste. Changing it means re-running course_retrieval_report.py."""
    assert MIN_COVERAGE == 0.5


def test_the_floor_costs_nothing_on_a_question_the_corpus_answers(corpus: Path) -> None:
    """Both readings of the same query, so the floor's cost is visible, not assumed."""
    index = DocIndex(load_pages(corpus, exclude=("quiz",)))
    query = "how does keyword search score a passage"
    assert index.search_scored(query, limit=3)
    assert index.search_scored(query, limit=3, min_coverage=MIN_COVERAGE)


def test_the_search_limit_is_capped(surface: CourseSurface) -> None:
    """A page budget is how a retrieval surface becomes a corpus download."""
    result = surface.call_tool("search_course", {"query": "loop graph", "limit": 999})
    assert len(result["hits"]) <= 10


def test_the_text_files_are_actually_SERVED_over_http(corpus: Path) -> None:
    """Wired is not reached. The host only built llms.txt for OpenAPI surfaces, so a
    document surface had an MCP endpoint and nothing a plain HTTP agent could read.
    This asserts the route exists, not that the builder was called."""
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    surface = build_course_surface(corpus)
    assert surface is not None
    app = build_multi_surface_app([("course", surface)])
    with TestClient(app) as client:
        index = client.get("/course/llms.txt")
        assert index.status_code == 200
        assert index.headers["content-type"].startswith("text/plain")
        full = client.get("/course/llms-full.txt")
        assert full.status_code == 200
        assert "transitions are legal" in full.text, "the corpus did not reach the file"
        # And the host index tells an agent the file is there.
        listed = client.get("/").json()
        course = next(s for s in listed["surfaces"] if s["name"] == "course")
        assert course["llms_txt"] == "/course/llms.txt"
