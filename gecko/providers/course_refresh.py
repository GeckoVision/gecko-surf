"""Keep the served course at the cohort's HEAD, without a redeploy.

WHY THIS EXISTS. The image bakes the course at build time. On 2026-09-28 the image was
built at 04:21 UTC and week 3 was published at 04:28, so /course/mcp served last
week's course to the class that started week 3 that morning, and nothing said so. A
course publishes weekly and the server deploys when the engine changes; tying the
first to the second means every publish needs somebody to remember a deploy.

WHAT IT DOES. Every ``GECKO_COURSE_REFRESH_SECONDS`` (default 600; 0 turns it off) it
reads the cohort repository's HEAD commit. Only when that differs from the commit the
surface serves does it download the tarball of THAT commit, build the corpus with the
surface's own allow and deny lists, and swap it in. The baked copy is the fallback:
any failure keeps whatever is served now.

WHAT IT REFUSES, and each is a way it could have gone wrong quietly:

- a head that is not a 40-hex commit (it goes into a URL);
- a tarball over ``MAX_TARBALL_BYTES``, or with a member that would land outside its
  folder (``tarfile``'s ``data`` filter);
- a download that yields no pages. An empty corpus answers "not covered" to every
  question, which reads like a correct refusal and is a broken fetch, so the old
  corpus stays (the surface's rule 2, held across a refresh).

CONTROL PLANE. Both URLs are constants naming a public repository; nothing a caller
sends reaches them. The corpus is published course prose, the same files a student
clones, and no user data.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ..doccorpus import DocIndex
from .course_surface import CourseSurface, build_course_surface

logger = logging.getLogger("gecko.course_refresh")

#: The published cohort. The Dockerfile's `course` stage and infra/deploy.sh name the
#: same repository; a new cohort changes all three.
COHORT_REPO = "Gecko-Academy/dev3pack-cohort-2026-09"
HEAD_URL = f"https://api.github.com/repos/{COHORT_REPO}/commits/main"
TARBALL_URL = f"https://codeload.github.com/{COHORT_REPO}/tar.gz/{{commit}}"

REFRESH_SECONDS_ENV = "GECKO_COURSE_REFRESH_SECONDS"
#: Ten minutes: a publish shows up within one class break, and the unauthenticated
#: GitHub API budget (60 requests an hour per address) is spent at 6 an hour.
DEFAULT_REFRESH_SECONDS = 600

#: The cohort tarball is about 7 MB; 64 MB is room to grow, not room to be abused.
MAX_TARBALL_BYTES = 64 * 1024 * 1024
TIMEOUT_SECONDS = 60

_COMMIT = re.compile(r"[0-9a-f]{40}")

Outcome = Literal["unchanged", "updated", "refused"]


class CourseRefreshError(Exception):
    """A refresh could not produce a corpus it is willing to serve."""


def refresh_seconds() -> int:
    """The interval, or 0 for off. A value that is not a whole number is the default."""
    raw = os.environ.get(REFRESH_SECONDS_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_REFRESH_SECONDS
    return max(0, value)


def fetch_head() -> str:
    """The cohort's HEAD commit, from the GitHub API."""
    request = urllib.request.Request(
        HEAD_URL, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - constant https URL
        body = json.loads(response.read(1024 * 1024))
    return str(body.get("sha", ""))


def extract_tarball(
    blob: bytes, dest: Path, *, max_bytes: int = MAX_TARBALL_BYTES
) -> Path:
    """Unpack a GitHub tarball into ``dest`` and return the repository root in it.

    GitHub wraps the tree in one ``<repo>-<commit>/`` folder, so the root is that
    folder, not ``dest``.
    """
    if len(blob) > max_bytes:
        raise CourseRefreshError(f"tarball is {len(blob)} bytes, over {max_bytes}")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise CourseRefreshError(f"tarball refused: {exc}") from exc
    folders = [child for child in dest.iterdir() if child.is_dir()]
    if len(folders) != 1:
        raise CourseRefreshError(f"expected one top folder, found {len(folders)}")
    return folders[0]


def download_commit(commit: str) -> Path:
    """Download and unpack ``commit``; the caller owns the returned folder's parent."""
    url = TARBALL_URL.format(commit=commit)
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - constant https URL, checked commit
        blob = response.read(MAX_TARBALL_BYTES + 1)
    dest = Path(tempfile.mkdtemp(prefix="gecko-course-"))
    try:
        return extract_tarball(blob, dest)
    except CourseRefreshError:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def refresh_once(
    surface: CourseSurface,
    *,
    head: Callable[[], str] = fetch_head,
    download: Callable[[str], Path] = download_commit,
) -> Outcome:
    """Bring ``surface`` to the cohort's HEAD if it is behind. One check, one swap."""
    commit = head().strip()
    if not _COMMIT.fullmatch(commit):
        raise CourseRefreshError("the cohort head is not a commit")
    if commit == surface.commit:
        return "unchanged"
    root = download(commit)
    try:
        fresh = build_course_surface(root, commit=commit)
    finally:
        # The index holds every page's text in memory, so the files are done with.
        shutil.rmtree(
            root.parent if root.parent.name.startswith("gecko-course-") else root,
            ignore_errors=True,
        )
    if fresh is None:
        logger.warning(
            "course %s has no pages; keeping %s", commit[:12], surface.commit[:12]
        )
        return "refused"
    index: DocIndex = fresh.index
    surface.replace(index, commit=commit)
    logger.info("course now at %s (%d pages)", commit[:12], len(index.pages))
    return "updated"


async def watch_loop(surface: CourseSurface, *, interval: float) -> None:
    """Refresh forever, until the server shuts down.

    A failed check is logged and retried next tick. It must not end the loop: a task
    started with ``create_task`` that raises dies silently, and a refresher that
    stopped at the first GitHub hiccup would be the same stale course with extra steps.
    """
    while True:
        try:
            await asyncio.to_thread(refresh_once, surface)
        except Exception as exc:  # noqa: BLE001 - logged, and the loop is the point
            logger.warning(
                "course refresh failed, serving %s: %s", surface.commit[:12], exc
            )
        await asyncio.sleep(interval)


__all__ = [
    "COHORT_REPO",
    "CourseRefreshError",
    "DEFAULT_REFRESH_SECONDS",
    "REFRESH_SECONDS_ENV",
    "extract_tarball",
    "fetch_head",
    "refresh_once",
    "refresh_seconds",
    "watch_loop",
]
