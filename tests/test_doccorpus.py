"""The document projector: what it loads, how it cuts, and what it refuses.

Every corpus here is written to a temp directory, because that is the module's only
way in. That is the point of the control-plane test below, and it is why these tests
are slightly more verbose than a `text=` parameter would have made them.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from gecko import doccorpus
from gecko.doccorpus import CorpusError, DocIndex, chunk_page, load_pages


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_page_id_is_the_path_and_title_comes_from_the_h1(tmp_path: Path) -> None:
    _write(tmp_path, "unit0/how-to-submit.md", "# Handing work in\n\nSave it first.\n")
    (page,) = load_pages(tmp_path)
    assert page.page_id == "unit0/how-to-submit"
    assert page.title == "Handing work in"
    assert page.tags == ("unit0",)
    assert page.text.startswith("# Handing work in")


def test_a_comment_header_wins_over_the_first_heading(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "a.md",
        "<!-- title: Structured Outputs -->\n<!-- tags: llm, json -->\n\n# Other\n\nBody.\n",
    )
    (page,) = load_pages(tmp_path)
    assert page.title == "Structured Outputs"
    assert page.tags == ("llm", "json")
    assert not page.text.startswith("<!--")


def test_excluded_words_and_empty_pages_never_enter_the_corpus(tmp_path: Path) -> None:
    _write(tmp_path, "unit1/lesson.md", "# Lesson\n\nBody.\n")
    _write(tmp_path, "unit1/quiz.md", "# Quiz\n\nThe answer is B.\n")
    _write(tmp_path, "unit1/empty.md", "\n\n")
    ids = [p.page_id for p in load_pages(tmp_path, exclude=("quiz",))]
    assert ids == ["unit1/lesson"]


def test_an_empty_directory_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(CorpusError):
        load_pages(tmp_path)
    with pytest.raises(CorpusError):
        load_pages(tmp_path / "nope")


def test_no_index_builder_accepts_raw_text() -> None:
    """CONTROL PLANE, BY CONSTRUCTION. The way into this index is a path the module
    read itself. If a `text=` / `content=` / `body=` parameter appeared on a builder,
    a response payload could be indexed and invariant #1 would be one caller away
    from broken — the door `surfacedoc` closed by construction.

    `DocPage`/`DocChunk` are RECORDS and of course hold text; what matters is that
    nothing in the engine builds one except the loader and the chunker (pinned
    below). That is a weaker guarantee than `surfacedoc`'s and it is stated as such
    rather than claimed away."""
    forbidden = {"text", "content", "body", "payload", "response"}
    builders = [
        doccorpus.load_pages,
        doccorpus.chunk_page,
        doccorpus.DocIndex.__init__,
        doccorpus.DocIndex.search_scored,
    ]
    for builder in builders:
        params = set(inspect.signature(builder).parameters)
        assert params.isdisjoint(forbidden), f"{builder.__name__} accepts raw text"


def test_the_loader_is_the_only_builder_of_a_page_in_the_engine() -> None:
    package = Path(inspect.getfile(doccorpus)).parent
    builders = [
        path
        for path in package.rglob("*.py")
        if "DocPage(" in path.read_text(encoding="utf-8")
    ]
    assert [p.name for p in builders] == ["doccorpus.py"]


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_an_oversized_block_keeps_all_of_itself_by_default(tmp_path: Path) -> None:
    """A truncated table keeps its header and loses its rows, so it reads as
    complete — silent AND authoritative, the worst shape. Default is to keep it."""
    table = "\n".join(f"| row {n} | value {n} |" for n in range(60))
    _write(tmp_path, "p.md", f"# T\n\nIntro.\n\n{table}\n")
    (page,) = load_pages(tmp_path)

    kept = chunk_page(page, 200)
    assert any(chunk.text.endswith("| row 59 | value 59 |") for chunk in kept)
    assert sum(len(c.text) for c in kept) >= len(table)

    cut = chunk_page(page, 200, oversize="truncate")
    assert not any(chunk.text.endswith("| row 59 | value 59 |") for chunk in cut)


def test_a_fenced_code_block_is_never_split(tmp_path: Path) -> None:
    code = "```bash\nuv run bootcamp check ch03\n\nuv run bootcamp submit ch03\n```"
    _write(tmp_path, "p.md", f"# T\n\nRun it.\n\n{code}\n\nDone.\n")
    (page,) = load_pages(tmp_path)
    chunks = chunk_page(page, 40)
    holding = [c for c in chunks if "```bash" in c.text]
    assert len(holding) == 1
    assert holding[0].text.count("```") == 2


def test_section_chunking_carries_the_heading_path(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "p.md",
        "# Page\n\nIntro.\n\n## Commands\n\nOne.\n\n### Submit\n\nTwo.\n\n## Marks\n\nThree.\n",
    )
    (page,) = load_pages(tmp_path)
    chunks = chunk_page(page, 800, sections=True)
    paths = [c.heading_path for c in chunks]
    assert ("Page", "Commands") in paths
    assert ("Page", "Commands", "Submit") in paths
    assert ("Page", "Marks") in paths
    # Ordinals are dense and in reading order, so a chunk id is stable and sortable.
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chunk_id == "p#000"


# --------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------


def _corpus(tmp_path: Path) -> list:
    _write(
        tmp_path,
        "unit0/how-to-submit.md",
        "# Handing work in\n\nSubmit the notebook you saved.\n\nThe checker re-runs it.\n",
    )
    _write(
        tmp_path,
        "unit0/get-settled.md",
        "# Get settled\n\nFork the repository, then clone your fork.\n",
    )
    return load_pages(tmp_path)


def test_the_index_refuses_rather_than_inventing_a_nearest_page(tmp_path: Path) -> None:
    """NOT IN THESE PAGES. A student's clone genuinely does not contain next week,
    and the never-empty prior would answer anyway — with a page picked without
    reference to the question."""
    index = DocIndex(_corpus(tmp_path))
    assert index.search_scored("quantum chromodynamics") == []
    assert index.search_scored("") == []


def test_the_index_finds_the_page_that_is_about_the_question(tmp_path: Path) -> None:
    index = DocIndex(_corpus(tmp_path))
    hits = index.search_scored("how do I hand my work in")
    assert hits[0].page_id == "unit0/how-to-submit"
    assert hits[0].score > 0
    assert hits[0].chunk.text  # the passage travels with the hit, not just its id


def test_one_per_source_is_a_post_rank_policy_and_switches_off(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "long.md",
        "# Fork\n\nFork the repository.\n\nFork it again.\n\nForking, continued.\n",
    )
    _write(tmp_path, "short.md", "# Cloning\n\nClone your fork.\n")
    index = DocIndex(load_pages(tmp_path), max_chars=40)
    grouped = index.search_scored("fork", limit=3)
    assert len(grouped) == len({hit.page_id for hit in grouped})
    ungrouped = index.search_scored("fork", limit=3, one_per_source=False)
    assert len(ungrouped) > len({hit.page_id for hit in ungrouped})


def test_a_hit_is_a_passage_and_never_a_call(tmp_path: Path) -> None:
    """No method, no path, no input schema: there is nothing on a `DocHit` an agent
    could mistake for something to invoke."""
    index = DocIndex(_corpus(tmp_path))
    hit = index.search_scored("fork")[0]
    assert not hasattr(hit, "method")
    assert not hasattr(hit, "inputSchema")
    assert not hasattr(hit.chunk, "operation")


def test_the_projector_never_builds_an_operation() -> None:
    """The ruling, as a test. A fabricated `Operation` for a page would be callable
    by type — `tools.to_tool` and `caller.prepare` accept any `Operation` — so the
    only durable guard is that this module cannot produce one."""
    tree = ast.parse(Path(inspect.getfile(doccorpus)).read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert names.isdisjoint({"Operation", "to_tool", "prepare", "tool_name"})
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported.isdisjoint({".ingest", ".tools", ".caller", "gecko.ingest"})
