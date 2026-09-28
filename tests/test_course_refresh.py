"""The course keeps up with the cohort it serves, and says which commit it is.

On 2026-09-28 the image was built at 04:21 UTC and week 3 was published at 04:28, so
/course/mcp served last week's course (219 pages, no unit 3) to a class that started
on week 3 — and the only way to tell was to count pages. These tests hold the two
fixes: the served corpus follows the cohort's HEAD without a redeploy, and the commit
it serves is stated rather than inferred.

Every network edge is injected; nothing here reaches GitHub.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from gecko.providers.course_refresh import (
    CourseRefreshError,
    extract_tarball,
    refresh_once,
    refresh_seconds,
)
from gecko.providers.course_surface import CourseSurface, build_course_surface

OLD = "a" * 40
NEW = "b" * 40


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        page = root / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")
    return root


WEEK2 = {"units/en/unit2/loops.mdx": "# Loops and graphs\n\nA loop repeats a step.\n"}
WEEK3 = {
    **WEEK2,
    "units/en/unit3/memory.mdx": "# State and memory\n\nEpisodic memory caps episodes.\n",
}


@pytest.fixture
def surface(tmp_path: Path) -> CourseSurface:
    built = build_course_surface(_write(tmp_path / "baked", WEEK2), commit=OLD)
    assert built is not None
    return built


def _downloader(tmp_path: Path, files: dict[str, str]):  # noqa: ANN202 - test helper
    calls: list[str] = []

    def download(commit: str) -> Path:
        calls.append(commit)
        return _write(tmp_path / f"dl-{commit[:6]}", files)

    return download, calls


def test_a_published_week_reaches_the_tools_without_a_redeploy(
    surface: CourseSurface, tmp_path: Path
) -> None:
    download, calls = _downloader(tmp_path, WEEK3)
    outcome = refresh_once(surface, head=lambda: NEW, download=download)
    assert outcome == "updated"
    assert calls == [NEW]
    assert surface.commit == NEW
    listed = surface.call_tool("list_course_pages", {"prefix": "units/en/unit3"})
    assert listed["count"] == 1
    hits = surface.call_tool("search_course", {"query": "episodic memory episodes"})
    assert hits["hits"][0]["page_id"] == "units/en/unit3/memory"


def test_an_unchanged_head_downloads_nothing(
    surface: CourseSurface, tmp_path: Path
) -> None:
    download, calls = _downloader(tmp_path, WEEK3)
    assert refresh_once(surface, head=lambda: OLD, download=download) == "unchanged"
    assert calls == []


def test_an_empty_download_never_replaces_a_real_corpus(
    surface: CourseSurface, tmp_path: Path
) -> None:
    """Rule 2 of the surface, held across a refresh: a corpus with no pages answers
    'not covered' to everything, which reads like a refusal and is a broken fetch."""
    download, _ = _downloader(tmp_path, {"README.txt": "not a page"})
    assert refresh_once(surface, head=lambda: NEW, download=download) == "refused"
    assert surface.commit == OLD
    assert surface.call_tool("list_course_pages", {})["count"] == 1


def test_a_head_that_is_not_a_commit_is_refused_before_any_download(
    surface: CourseSurface, tmp_path: Path
) -> None:
    """The head goes into a URL. Only a 40-hex commit may."""
    download, calls = _downloader(tmp_path, WEEK3)
    with pytest.raises(CourseRefreshError):
        refresh_once(surface, head=lambda: "main/../../evil", download=download)
    assert calls == []


def test_the_served_commit_is_stated_not_inferred(surface: CourseSurface) -> None:
    assert surface.call_tool("list_course_pages", {})["commit"] == OLD
    assert OLD in surface.artifacts()["llms.txt"]


def test_a_refresh_reaches_the_http_files_too(
    surface: CourseSurface, tmp_path: Path
) -> None:
    """The tools followed the swap while the files did not: the host froze every
    artifact into a route at mount time, so llms.txt kept the old map and a new
    page had no route at all."""
    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    app = build_multi_surface_app([("course", surface)])
    download, _ = _downloader(tmp_path, WEEK3)
    with TestClient(app) as client:
        new_page = "/course/pages/units/en/unit3/memory.md"
        assert client.get(new_page).status_code == 404
        refresh_once(surface, head=lambda: NEW, download=download)
        assert NEW in client.get("/course/llms.txt").text
        assert "Episodic memory" in client.get("/course/llms-full.txt").text
        page = client.get(new_page)
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/markdown")


def _tarball(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_the_tarball_is_unwrapped_to_the_repository_root(tmp_path: Path) -> None:
    blob = _tarball({"cohort-abc/units/en/unit3/memory.mdx": b"# Memory\n"})
    root = extract_tarball(blob, tmp_path / "out")
    assert (root / "units/en/unit3/memory.mdx").read_text() == "# Memory\n"


def test_a_member_that_escapes_the_folder_is_refused(tmp_path: Path) -> None:
    blob = _tarball({"cohort-abc/../../escaped.mdx": b"# no\n"})
    with pytest.raises(CourseRefreshError):
        extract_tarball(blob, tmp_path / "out")
    assert not (tmp_path / "escaped.mdx").exists()


def test_an_oversized_download_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CourseRefreshError):
        extract_tarball(b"x" * 64, tmp_path / "out", max_bytes=32)


def test_the_refresh_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_COURSE_REFRESH_SECONDS", raising=False)
    assert refresh_seconds() == 600
    monkeypatch.setenv("GECKO_COURSE_REFRESH_SECONDS", "0")
    assert refresh_seconds() == 0
    monkeypatch.setenv("GECKO_COURSE_REFRESH_SECONDS", "junk")
    assert refresh_seconds() == 600
