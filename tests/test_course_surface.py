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
    assert (
        surface.call_tool("list_course_pages", {"prefix": "units/en/unit1"})["count"]
        == 2
    )
    assert (
        surface.call_tool("list_course_pages", {"prefix": "units/en/unit9"})["count"]
        == 0
    )


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
    # The two text files, plus one file per page so the map's links resolve.
    assert {"llms.txt", "llms-full.txt"} <= set(artifacts)
    assert set(artifacts) - {"llms.txt", "llms-full.txt"} == {
        f"pages/{page_id}.md" for page_id in surface.index.pages
    }
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


# -- what the publisher withholds, this surface must withhold ------------------


@pytest.fixture
def repo_shaped(tmp_path: Path) -> Path:
    """A corpus laid out like the real course repo, machinery included.

    Every path here was in the corpus on 2026-09-25 and had no business being there.
    """
    files = {
        "units/en/unit1/lesson.mdx": "# Redaction\n\nHow to write a redact helper.\n",
        "projects/01-clothing-reviews/README.md": "# Clothing reviews\n\nThe project.\n",
        "docs/guides/setup.md": "# Setup\n\nInstall the toolchain.\n",
        "docs/curriculum.md": "# Curriculum\n\nThe sessions in order.\n",
        # --- none of the following may ever be served ---
        "docs/instructor/assessment-rubric.md": "# Rubric\n\nAward 500 points when.\n",
        # Inside an ALLOWED prefix, so only the deny list can stop these two. Without
        # them the allow-list narrowing alone carries the instructor tests and the
        # `solutions` -> `solution` fix is never exercised.
        "units/en/unit1/solutions/answer.md": (
            "# Answer\n\nThe gated worked answer for unit one.\n"
        ),
        "projects/01-clothing-reviews/solution.md": (
            "# Solution\n\nThe worked answer sitting beside the project.\n"
        ),
        "docs/instructor/projects/01-clothing-reviews/solution.md": (
            "# Solution\n\nThe worked answer to the clothing reviews project.\n"
        ),
        "docs/specs/internal-design.md": "# Spec\n\nInternal design record.\n",
        "docs/plans/next-week.md": "# Plan\n\nInternal planning note.\n",
        "integrations/sendai-txs/node_modules/left-pad/README.md": (
            "# left-pad\n\nSomebody else's dependency readme.\n"
        ),
        "integrations/gecko-orquestra/README.md": "# Integration\n\nRun the demo.\n",
    }
    for rel, text in files.items():
        page = tmp_path / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")
    return tmp_path


def test_the_instructor_solution_is_not_servable(repo_shaped: Path) -> None:
    """The finding that stopped this branch merging.

    Measured 2026-09-25 against the real course: `docs/instructor/projects/
    01-clothing-reviews/solution` was the TOP hit for "clothing reviews project
    solution", readable by `read_course_page`, and present in `llms-full.txt`. The
    deny list said `solutions` while the file is called `solution.md`, and the allow
    list admitted a bare `docs/`, which is where the instructor's material lives next
    to the learner's.
    """
    surface = build_course_surface(repo_shaped)
    assert surface is not None
    solution = "docs/instructor/projects/01-clothing-reviews/solution"
    assert solution not in surface.index.pages
    assert (
        surface.call_tool("read_course_page", {"page_id": solution})["found"] is False
    )
    hits = surface.call_tool(
        "search_course", {"query": "clothing reviews project solution"}
    )["hits"]
    assert not [h for h in hits if "instructor" in h["page_id"]]
    assert "worked answer" not in surface.artifacts()["llms-full.txt"]


def test_a_solution_inside_an_allowed_prefix_is_still_refused(
    repo_shaped: Path,
) -> None:
    """The deny list on its own, with nothing else able to carry it.

    `units/` and `projects/` are both allowed, so the allow list cannot refuse these.
    Only `EXCLUDE` can. It said `solutions` (plural) until 2026-09-25 while the real
    file is `solution.md`, which is how a deny list named a thing that does not exist
    and served the thing that does.
    """
    surface = build_course_surface(repo_shaped)
    assert surface is not None
    assert "units/en/unit1/solutions/answer" not in surface.index.pages
    assert "projects/01-clothing-reviews/solution" not in surface.index.pages
    full = surface.artifacts()["llms-full.txt"]
    assert "gated worked answer" not in full
    assert "sitting beside the project" not in full
    # The lesson and the project README beside them still load: a deny list that takes
    # the exercise with its answer has not fixed anything.
    assert "units/en/unit1/lesson" in surface.index.pages
    assert "projects/01-clothing-reviews/README" in surface.index.pages


def test_nothing_the_publisher_withholds_is_served(repo_shaped: Path) -> None:
    """`scripts/publish_cohort.py`'s `NEVER` set, asserted from this side.

    Our deny list was written from memory while the course already declared one. A
    page the student's own clone never receives must not be reachable here either.
    """
    surface = build_course_surface(repo_shaped)
    assert surface is not None
    withheld = ("docs/instructor", "docs/specs", "docs/plans")
    served = [
        page_id for page_id in surface.index.pages if page_id.startswith(withheld)
    ]
    assert served == [], f"the publisher withholds these and we served them: {served}"


def test_a_dependency_tree_inside_the_scope_is_refused(repo_shaped: Path) -> None:
    """An allow list scopes what COUNTS as the course. It cannot see that a directory
    inside that scope is somebody else's package. `integrations/` is course content
    and `integrations/sendai-txs/node_modules` was 468 pages of vendor README."""
    surface = build_course_surface(repo_shaped)
    assert surface is not None
    assert not [p for p in surface.index.pages if "node_modules" in p]
    # ...and the sibling that IS course content still loads, so the deny is not a
    # blanket refusal of the directory it sits in.
    assert "integrations/gecko-orquestra/README" in surface.index.pages


def test_the_learner_facing_docs_still_load(repo_shaped: Path) -> None:
    """Narrowing `docs/` must not cost the guides. A deny list that takes the answer
    with the answer key has not fixed anything."""
    surface = build_course_surface(repo_shaped)
    assert surface is not None
    assert "docs/guides/setup" in surface.index.pages
    assert "docs/curriculum" in surface.index.pages


def test_every_link_in_the_map_resolves(corpus: Path) -> None:
    """The defect the per-page route exists for.

    `llms.txt` listed every page and linked each one to its own page id, which is not
    a URL and matched no route. For the client these files are written for, the one
    that cannot speak MCP and so cannot call `read_course_page`, the whole "Pages"
    section was decorative and the only usable artifact was the entire corpus in one
    fetch. This walks the map and fetches what it names.
    """
    import re

    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    surface = build_course_surface(corpus)
    assert surface is not None
    app = build_multi_surface_app([("course", surface)])
    with TestClient(app) as client:
        index = client.get("/course/llms.txt")
        assert index.status_code == 200
        links = re.findall(r"^- \[[^\]]+\]\(([^)]+)\)", index.text, re.MULTILINE)
        assert links, "the map listed no pages"
        for link in links:
            page = client.get(f"/course/{link}")
            assert page.status_code == 200, f"{link} is in the map and answers 404"
            assert page.headers["content-type"].startswith("text/markdown")
            assert page.text.strip(), f"{link} resolved to an empty page"


def test_a_page_the_surface_refuses_has_no_route_either(repo_shaped: Path) -> None:
    """A deny list that only reaches the MCP tools is not a deny list. If the pages
    are served as files, an excluded page must 404 as a file too."""
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    surface = build_course_surface(repo_shaped)
    assert surface is not None
    app = build_multi_surface_app([("course", surface)])
    with TestClient(app) as client:
        for withheld in (
            "pages/docs/instructor/assessment-rubric.md",
            "pages/projects/01-clothing-reviews/solution.md",
            "pages/units/en/unit1/solutions/answer.md",
        ):
            assert client.get(f"/course/{withheld}").status_code == 404, withheld
        # The lesson beside them is still served, so this is a deny and not an outage.
        assert client.get("/course/pages/units/en/unit1/lesson.md").status_code == 200


def test_the_host_refuses_an_artifact_name_it_cannot_place() -> None:
    """The host now accepts an OPEN set of artifact names from a surface, so the shape
    is what is checked. These names never occur today; they are refused so that the
    day a corpus root points somewhere else, a page id cannot become a path."""
    from gecko.http_server import build_multi_surface_app

    class Hostile:
        instructions = "x"

        def list_tools(self) -> list[dict[str, object]]:
            return []

        def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            raise AssertionError("not called")

        def artifacts(self) -> dict[str, str]:
            return {
                "/etc/passwd": "root:x:0:0",
                "pages/../../secret.md": "no",
                "pages/run.sh": "#!/bin/sh",
                "pages/ok.md": "# fine",
            }

    app = build_multi_surface_app([("h", Hostile())])
    # Each surface is MOUNTED, so its routes live under the mount rather than on the
    # app. Reading `app.routes` alone finds nothing and the assertion passes vacuously.
    mount = next(r for r in app.routes if getattr(r, "path", "") == "/h")
    served = {getattr(route, "path", "") for route in mount.routes}
    assert "/pages/ok.md" in served
    assert not [p for p in served if "etc" in p or ".." in p or p.endswith(".sh")]
