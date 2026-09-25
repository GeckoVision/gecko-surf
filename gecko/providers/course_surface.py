"""The course surface — the Dev3Pack lessons, answerable by an agent a student owns.

WHAT PROBLEM THIS SOLVES. A student who wants their own assistant to help them with
the course has to paste a URL per page, one at a time, and the assistant still cannot
tell which page answers a question it was not handed. This surface is the other
shape of that: one connection, and the pages become searchable.

WHY IT IS NOT A CATALOG SURFACE. Everything else this host serves is CALLABLE — a
program instruction, an endpoint, a purchase. A lesson is not. ``gecko.doccorpus``
exists precisely so a page can be ranked without being projected into an
``Operation``, and this surface is the reason that distinction had to hold: nothing
here can reach ``tools.to_tool`` or ``caller.prepare``, and that is enforced by the
type rather than by a promise.

THREE RULES THIS SURFACE KEEPS, and each one is a way it could have lied instead:

1. **Empty is an answer.** ``DocIndex`` ships no never-empty fallback, and this
   surface does not add one. A corpus that has not published week 3 genuinely does
   not contain week 3, and handing back the closest week-1 page with the same shape
   as a real hit is how a student ends up confidently wrong. ``search_course``
   returns ``hits: []`` and says so.
2. **It refuses to mount empty.** A surface with no pages would answer "not in these
   pages" to every question, which reads exactly like a correct refusal and is
   actually a broken deploy. :func:`build_course_surface` returns ``None`` when the
   corpus is missing, so the mount never appears rather than appearing broken.
3. **No quizzes.** ``load_pages`` is called with ``exclude=("quiz",)``. A quiz page
   is an answer key, and an answer key that is retrievable is an answer key that is
   retrieved.

CONTROL PLANE, unchanged. The corpus is read from a path on this server at build
time. No tool takes text, a URL, or a path from the caller, so nothing a caller says
can make this read a file or reach the network. The surface holds published course
prose and no user data of any kind.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..doccorpus import CorpusError, DocIndex, load_pages
from ..tools import tool_annotations

#: Where the course markdown lives on this server. A deploy sets it; nothing else does.
COURSE_ROOT_ENV = "GECKO_COURSE_ROOT"

#: What counts as the course. An ALLOW list, because a corpus taken from a
#: repository otherwise gets the repository's machinery: on 2026-09-24 the top hit
#: for "how do I install uv" was `.claude/skills/fix-my-setup/SKILL` and for "which
#: terminal should I use" it was `.claude/agents/setup-doctor`. Both match well and
#: neither is something to hand a student. Everything not named here is out.
INCLUDE = (
    "units/",  # the lessons
    "projects/",  # the projects, their READMEs and their notebooks
    "cookbook/",  # worked recipes
    "demos/",  # runnable demonstrations
    "depth/",  # the depth track
    "ship-it/",  # the ship-it track
    "capstone-template/",
    "final_assignment/",
    "integrations/",
    # NOT a bare `docs/`. That directory holds the instructor's material next to the
    # learner's, and on 2026-09-25 a bare prefix put `docs/instructor/projects/
    # 01-clothing-reviews/solution` at the TOP of the hits for "clothing reviews
    # project solution", readable by id and sitting inside `llms-full.txt`.
    "docs/guides/",
    "docs/dev3pack/",
    "docs/curriculum",
    "docs/course-index",
    "docs/notebook-index",
    "README",
    "SETUP",
)

#: Never loaded, whatever the allow list says. A quiz competes with the lesson that
#: teaches it; a solution is an answer key, and an answer key that is retrievable is
#: retrieved. Both are in the student's own clone either way.
#:
#: `solution` is SINGULAR on purpose. It was `solutions` until 2026-09-25 and the file
#: is called `solution.md`, so the deny list named a thing that does not exist while the
#: thing that does exist was served. A substring match on the singular catches both.
#: `instructor` is the same lesson applied one level up: the rubric, the session plans
#: and every deck live under it.
#:
#: THIS LIST IS NOT OURS TO INVENT. The course's own publisher already declares what
#: never travels to a student (`scripts/publish_cohort.py`, the `NEVER` mapping:
#: `tests` — "test_checks.py holds the solved value of every exercise" — plus
#: `docs/instructor`, `docs/specs`, `docs/plans`, `evals`, `.github`). We were serving
#: what that contract withholds, because we wrote a second list from memory instead of
#: reading the first. The allow list above is narrowed to agree with it; if the two ever
#: disagree again, the publisher's is right and this one is the bug.
EXCLUDE = ("quiz", "solution", "instructor")

#: Machinery, refused whatever the allow list says. An allow list scopes what COUNTS as
#: the course; it cannot see that a directory inside the scope is a dependency tree.
#: `integrations/` is course content and `integrations/sendai-txs/node_modules` is 468
#: pages of somebody else's README, which the allow list happily admitted.
DENY_DIRS = (
    "node_modules",
    "site-packages",
    ".venv",
    "__pycache__",
    ".ipynb_checkpoints",
    "graphify-out",
)

#: The text shapes a course is written in. Notebooks are projected, not read.
SUFFIXES = (".md", ".mdx", ".ipynb")

#: Where a single page is served, relative to this surface's mount. Every link in
#: `llms.txt` points here, so an agent that read the map can fetch what it names.
PAGES_PREFIX = "pages"

#: A hit must contain at least half the query's content terms. MEASURED 2026-09-24,
#: not chosen: at 0.5 the course's own 49 labelled questions still score 38/49, exactly
#: what they score with no floor, while the out-of-scope pass rate goes from 23% to 85%
#: on a 13-question smoke set. Above 0.5 recall starts paying (0.67 costs 3 questions).
#: Before this existed, "how do I bake sourdough bread at home" returned a course page
#: because one term, `home`, appears in the prose.
#:
#: The weak half of that evidence is the out-of-scope set: 13 questions written in one
#: sitting by the person who then picked the threshold. The 38/49 is somebody else's
#: labels; this is not. Treat 0.5 as the best available answer, not a settled one.
MIN_COVERAGE = 0.5

#: Cap on what one search returns. Not a tuning knob: a bigger page budget is how a
#: retrieval surface quietly turns into a way to download the corpus a chunk at a time.
MAX_LIMIT = 10

SEARCH_TOOL = {
    "name": "search_course",
    "annotations": tool_annotations(read_only=True, title="Search the course"),
    "description": (
        "Ask the Dev3Pack AI-Engineering course a question in plain words and get the "
        "passages that answer it, each with the page it came from so you can cite it. "
        "AN EMPTY RESULT IS AN ANSWER: it means these pages do not cover it, which is "
        "the honest reply when a student asks about a week that has not been published "
        "yet. Do not fill that silence with a guess; say the course does not cover it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The question, in the words a student would use.",
                "minLength": 2,
                "maxLength": 500,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": f"How many passages to return (1-{MAX_LIMIT}, default 5).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

READ_TOOL = {
    "name": "read_course_page",
    "annotations": tool_annotations(read_only=True, title="Read one course page"),
    "description": (
        "Read one full course page by the `page_id` a search hit gave you. Use this "
        "when a passage looks right but you need what surrounds it. The id must be one "
        "this surface already returned; anything else is refused rather than guessed at."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "The id from a search hit, e.g. 'unit2/session-08-loops-and-graphs/concepts-1'.",
                "maxLength": 300,
            }
        },
        "required": ["page_id"],
        "additionalProperties": False,
    },
}

LIST_TOOL = {
    "name": "list_course_pages",
    "annotations": tool_annotations(read_only=True, title="List the published pages"),
    "description": (
        "Every page this surface holds, with its id and title. Use it to see what the "
        "course covers before searching, and to tell 'not published yet' from 'not "
        "found'. The list is what a student's own clone would contain, nothing more."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "description": "Only ids starting with this, e.g. 'unit2' or 'unit2/session-09'.",
                "maxLength": 200,
            }
        },
        "additionalProperties": False,
    },
}


@dataclass
class CourseSurface:
    """Duck-typed MCP surface over a document corpus: search, read, list.

    ``index`` is injected so every test runs against a corpus it built itself, with
    no network and no dependence on what happens to be deployed.
    """

    index: DocIndex
    pages_root: str = ""
    _titles: dict[str, str] = field(default_factory=dict, init=False)

    surface_id = "gecko:course"

    instructions = (
        "The Dev3Pack AI-Engineering course, searchable. These are the same lesson "
        "pages a student reads, served so you can find the one that answers a question "
        "instead of being handed a URL at a time.\n"
        "\n"
        "HOW TO USE IT:\n"
        "1. `search_course` with the student's actual question, in their words. Each hit "
        "carries a `page_id` and the heading it sits under — cite those, because a "
        "student can open them.\n"
        "2. `read_course_page` when a passage looks right and you need its surroundings.\n"
        "3. `list_course_pages` to see what exists before deciding something is missing.\n"
        "\n"
        "THE ONE RULE THAT MATTERS: an empty result is an answer. This corpus holds "
        "what has been published, and a course runs week by week. If a search returns "
        "nothing, tell the student the course does not cover it yet and say which pages "
        "do exist. Do not answer from your own knowledge and let them believe they read "
        "it here: they will look for it, and it will not be there."
    )

    def __post_init__(self) -> None:
        self._titles = {
            page_id: page.title for page_id, page in self.index.pages.items()
        }

    # -- tools ---------------------------------------------------------------

    # -- agent-readable text, for clients that cannot speak MCP ----------------

    def artifacts(self) -> dict[str, str]:
        """``llms.txt`` and ``llms-full.txt``, built from the same corpus the tools rank.

        WHY BOTH. ``llms.txt`` is a map: an agent reads it and knows what exists and
        where to ask. ``llms-full.txt`` is the corpus itself, in one fetch, and it is
        the file that actually solves "a student pastes eleven URLs one at a time".
        A client that cannot connect to MCP can still read a course this way.

        Built from ``self.index``, so a page the tools refuse to rank (a quiz) is a
        page these files cannot leak either. One corpus, one set of exclusions.
        """
        files = {"llms.txt": self._llms_txt(), "llms-full.txt": self._llms_full_txt()}
        # ONE FILE PER PAGE, so the map is followable. `llms.txt` listed every page and
        # linked each one to its own id, which is not a URL: over plain HTTP the entire
        # "Pages" section was decorative, and the only usable artifact was the whole
        # corpus in one 700 KB fetch. `read_course_page` solved this for MCP clients
        # only — the clients these files exist for are exactly the ones that cannot
        # call it.
        for page_id, page in self.index.pages.items():
            files[f"{PAGES_PREFIX}/{page_id}.md"] = f"# {page.title}\n\n{page.text}"
        return files

    def _llms_txt(self) -> str:
        lines = [
            "# Dev3Pack AI-Engineering",
            "",
            "> The course, served so an agent can search it instead of being handed one "
            "URL at a time. These are the published lesson pages; a course runs week by "
            "week, so what is missing here is not yet released rather than hidden.",
            "",
            "## How to use this",
            "",
            "- Connect over MCP and call `search_course` for a question in a student's "
            "own words. An empty result means the course does not cover it yet.",
            "- Or fetch any single page at the link beside its title below.",
            "- Or fetch `llms-full.txt` beside this file for every page in one request.",
            "",
            f"## Pages ({len(self._titles)})",
            "",
        ]
        lines += [
            f"- [{title}]({PAGES_PREFIX}/{page_id}.md): {page_id}"
            for page_id, title in sorted(self._titles.items())
        ]
        return "\n".join(lines) + "\n"

    def _llms_full_txt(self) -> str:
        parts = [
            "# Dev3Pack AI-Engineering — every published page",
            "",
            "One file so an agent does not have to walk the course link by link. Pages "
            "are separated by a rule and named by the id you would cite.",
            "",
        ]
        for page_id, page in sorted(self.index.pages.items()):
            parts += ["---", "", f"# {page.title}", f"id: {page_id}", "", page.text, ""]
        return "\n".join(parts) + "\n"

    def list_tools(self, **_kwargs: Any) -> list[dict[str, Any]]:
        # `search_course` first: it is the one an agent should reach for, and the
        # order a tool list is read in is part of its interface.
        return [SEARCH_TOOL, READ_TOOL, LIST_TOOL]

    def call_tool(self, name: str, arguments: dict[str, Any], **_kwargs: Any) -> Any:
        args = arguments or {}
        if name == "search_course":
            return self._search(args)
        if name == "read_course_page":
            return self._read(args)
        if name == "list_course_pages":
            return self._list(args)
        raise ValueError(f"unknown tool: {name}")

    # -- implementations -----------------------------------------------------

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"query": "", "hits": [], "note": "no query given"}
        limit = min(int(args.get("limit") or 5), MAX_LIMIT)
        hits = self.index.search_scored(query, limit=limit, min_coverage=MIN_COVERAGE)
        return {
            "query": query,
            "pages_searched": len(self.index.pages),
            "hits": [
                {
                    "page_id": hit.page_id,
                    "title": hit.page.title,
                    "heading": " > ".join(hit.chunk.heading_path),
                    "text": hit.chunk.text,
                    "score": hit.score,
                }
                for hit in hits
            ],
            # Said in the payload, not only in the tool description: a model that
            # skipped the description still reads the thing it got back.
            "note": (
                "no passage in these pages matches; the course may not cover this yet"
                if not hits
                else "cite page_id; a student can open it"
            ),
        }

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        page_id = str(args.get("page_id") or "").strip()
        page = self.index.pages.get(page_id)
        if page is None:
            # Name a few real ids rather than only refusing: the usual cause is a
            # near-miss id, and a refusal that cannot be acted on costs another turn.
            near = [
                known for known in sorted(self._titles) if page_id and page_id in known
            ]
            return {
                "page_id": page_id,
                "found": False,
                "note": "no page with that id; use list_course_pages or search_course",
                "did_you_mean": near[:5],
            }
        return {
            "page_id": page.page_id,
            "found": True,
            "title": page.title,
            "tags": list(page.tags),
            "text": page.text,
        }

    def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        prefix = str(args.get("prefix") or "")
        rows = [
            {"page_id": page_id, "title": title}
            for page_id, title in sorted(self._titles.items())
            if page_id.startswith(prefix)
        ]
        return {"prefix": prefix, "count": len(rows), "pages": rows}


def course_root() -> Path | None:
    """The corpus path a deploy set, or ``None``. The only place the env is read."""
    raw = os.environ.get(COURSE_ROOT_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    return root if root.is_dir() else None


def build_course_surface(root: Path | None = None) -> CourseSurface | None:
    """The surface, or ``None`` when there is no corpus to serve.

    Returning ``None`` rather than an empty surface is the whole point: an empty
    corpus answers "not in these pages" to everything, which is indistinguishable
    from a correct refusal and is actually a broken deploy. A mount that does not
    appear is a deploy problem somebody notices.
    """
    root = root or course_root()
    if root is None or not root.is_dir():
        return None
    try:
        pages = load_pages(
            root,
            suffixes=SUFFIXES,
            include=INCLUDE,
            exclude=EXCLUDE + DENY_DIRS,
        )
    except CorpusError:
        # The loader raises on a root with no pages. Here that is not an error to
        # propagate: it is the deploy saying there is nothing to serve, and the
        # answer is to not mount rather than to crash the whole host.
        return None
    if not pages:
        return None
    return CourseSurface(index=DocIndex(pages), pages_root=str(root))
