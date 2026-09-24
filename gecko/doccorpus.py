"""Documents as rankable units — the second projector onto the shipped lexical arm.

A page is not an endpoint, and this module exists so nobody has to pretend it is.
It projects markdown pages into :class:`~gecko.rankable.RankableUnit`, which is the
only thing the lexical scorer has ever actually read. Nothing here produces an
``Operation``, so nothing here can reach ``tools.to_tool`` or ``caller.prepare``:
**rankable is not callable**, and that is enforced by the type, not by a comment.

THREE DECISIONS, ALL DELIBERATE, ALL MEASURABLE (``scripts/course_retrieval_report.py``):

1. **Chunking lives HERE, at the projector** — above ``Catalog`` and above any dense
   index. Put a chunker inside either one and the two arms get two namespaces, the
   RRF join in ``search.py`` (which fuses on the unit name) degrades to one arm, and
   nothing goes red. One chunk = one unit = one id, for every arm.
2. **No never-empty fallback.** The 0/97 prior exists because an API surface that
   returns nothing looks broken. A corpus that answers "not in these pages" is
   telling the truth — a student's clone genuinely does not contain next week — and a
   query-independent prior would hand them a week-2 page for a week-9 question with
   the same confidence as a real hit.
3. **No intent gate.** On an API surface a body-only match is prose brushing (the
   OHLC *open* value answering "restaurants open tonight"), so a hit must corroborate
   on summary/tags/operationId. In a lesson the body IS the answer, and gating on the
   heading refuses every page that teaches something its title does not name. Measured
   on the course's own 49 labelled questions — see the report.

CONTROL PLANE. The loader takes a PATH and reads it. No builder here — not
``load_pages``, not ``chunk_page``, not ``DocIndex`` — takes a ``text=`` argument
through which a response body, a user's data, or anything an agent fetched could
enter the index. ``DocPage`` is a record and of course holds text; the guarantee is
that the loader is the only thing in the engine that makes one, which a test pins.
That is weaker than ``gecko.surfacedoc``'s "there is no such parameter at all", and
it is said plainly rather than claimed away — a free-text ingest door is the obvious
wrong answer waiting to be taken while the V2 feedback path is unresolved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .rankable import FoldedUnit, RankableUnit, Tokenize, fold_unit, rank_units

__all__ = [
    "DocChunk",
    "DocHit",
    "DocIndex",
    "DocPage",
    "chunk_page",
    "load_pages",
    "unit_for_chunk",
]

#: How an oversized block (one longer than the whole chunk budget) is handled.
#: ``keep`` gives it its own chunk, whole. ``truncate`` reproduces the course
#: chunker's behaviour, which cuts it — and a cut table keeps its header and loses
#: its rows, so it reads as complete. Kept only so the two can be MEASURED against
#: each other rather than argued about.
Oversize = Literal["keep", "truncate"]

_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
#: ``<!-- title: ... -->`` / ``<!-- tags: a, b -->`` front matter, the teaching
#: corpus format. Optional: a page whose first heading is its title works too.
_COMMENT_HEADER = re.compile(r"^<!--\s*(title|tags)\s*:\s*(.+?)\s*-->\s*$")


class CorpusError(Exception):
    """The corpus directory or one of its pages is unusable."""


@dataclass(frozen=True)
class DocPage:
    """One document. ``page_id`` is its stable, navigable identity — the thing a
    citation names and a reader can open."""

    page_id: str
    title: str
    text: str
    source: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocChunk:
    """A ranked slice of one page. The unit of retrieval; never a unit of action."""

    page_id: str
    ordinal: int
    text: str
    #: The markdown heading stack this slice sits under, outermost first. Empty for
    #: the paragraph chunker, which does not look at structure.
    heading_path: tuple[str, ...] = ()

    @property
    def chunk_id(self) -> str:
        # Zero-padded so the id sorts in reading order as a string, which is what
        # the ranker's deterministic tie-break compares.
        return f"{self.page_id}#{self.ordinal:03d}"


# --------------------------------------------------------------------------
# Loading — path in, pages out. No text parameter, on purpose.
# --------------------------------------------------------------------------


def _read_header(lines: list[str]) -> tuple[str | None, tuple[str, ...], int]:
    title: str | None = None
    tags: tuple[str, ...] = ()
    body_start = 0
    for index, line in enumerate(lines):
        match = _COMMENT_HEADER.match(line)
        if match is None:
            body_start = index
            break
        key, value = match.group(1), match.group(2)
        if key == "title":
            title = value
        else:
            tags = tuple(tag.strip() for tag in value.split(",") if tag.strip())
        body_start = index + 1
    return title, tags, body_start


def _first_heading(lines: Iterable[str]) -> str | None:
    for line in lines:
        match = _HEADING.match(line)
        if match is not None:
            return match.group(2).strip()
    return None


def load_pages(
    root: Path,
    *,
    suffixes: Sequence[str] = (".md", ".mdx"),
    exclude: Sequence[str] = (),
    tag_from_parent: bool = True,
) -> list[DocPage]:
    """Every page under ``root``, sorted by ``page_id`` for determinism.

    ``page_id`` is the path relative to ``root`` without its suffix, so it is both
    an identity and something a reader can resolve back to a file (and, once a
    corpus is published, to a URL — a projector this module deliberately does not
    guess at, because a server filesystem path must never leave the process).

    ``exclude`` drops any page whose id contains one of the given words. The course
    corpus uses it for ``quiz``: an answer key must not be retrievable.

    Built per call and never cached to disk: an index that can go stale is worse
    than no index.
    """
    base = root.resolve()
    if not base.is_dir():
        raise CorpusError(f"corpus directory not found: {root}")
    pages: list[DocPage] = []
    for path in sorted(base.rglob("*")):
        if path.suffix not in suffixes or not path.is_file():
            continue
        # A symlink pointing out of the corpus would pull an arbitrary file into a
        # surface an agent can read. Ingested content is untrusted; so is its shape.
        if not path.resolve().is_relative_to(base):
            continue
        page_id = path.relative_to(base).with_suffix("").as_posix()
        if any(word in page_id for word in exclude):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        title, tags, body_start = _read_header(lines)
        body = "\n".join(lines[body_start:]).strip()
        if not body:
            continue
        if title is None:
            title = _first_heading(lines[body_start:]) or Path(page_id).name
        if not tags and tag_from_parent:
            parent = Path(page_id).parent.as_posix()
            tags = () if parent == "." else (parent,)
        pages.append(
            DocPage(
                page_id=page_id,
                title=title,
                text=body,
                source=str(path),
                tags=tags,
            )
        )
    if not pages:
        raise CorpusError(f"no {'/'.join(suffixes)} pages under {root}")
    return pages


# --------------------------------------------------------------------------
# Chunking — the projector's own step, measured, not assumed
# --------------------------------------------------------------------------


def _blocks(text: str) -> list[tuple[str, tuple[int, str] | None]]:
    """Paragraph blocks, with a fenced code block kept ATOMIC and a heading line
    emitted as its own block carrying ``(level, text)``.

    A fence is atomic because a blank line inside one is not a paragraph break, and
    half a command is worse than no command. A table needs no special case: its rows
    are contiguous non-blank lines, so it is already one block — what breaks a table
    is the packer's truncation, not the splitter (see ``Oversize``).
    """
    blocks: list[tuple[str, tuple[int, str] | None]] = []
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        joined = "\n".join(current).strip()
        if joined:
            blocks.append((joined, None))
        current.clear()

    for line in text.splitlines():
        if fence is not None:
            current.append(line)
            if _FENCE.match(line) and line.strip().startswith(fence):
                fence = None
            continue
        opening = _FENCE.match(line)
        if opening is not None:
            fence = opening.group(1)
            current.append(line)
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            flush()
            blocks.append((line.strip(), (len(heading.group(1)), heading.group(2))))
            continue
        if not line.strip():
            flush()
            continue
        current.append(line)
    flush()
    return blocks


def _pack(blocks: Sequence[str], max_chars: int, oversize: Oversize) -> list[list[str]]:
    """Pack whole blocks into groups of at most ``max_chars``. The course chunker's
    shape (paragraph packing, never a mid-sentence cut), with the one behaviour it
    got wrong made selectable rather than inherited."""
    packed: list[list[str]] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        raw_len = len(block)
        if length and length + raw_len + 2 > max_chars:
            packed.append(current)
            current, length = [], 0
        if raw_len > max_chars:
            if oversize == "keep":
                if current:
                    packed.append(current)
                    current, length = [], 0
                packed.append([block])
                continue
            # The course chunker: cut the block and still count its FULL length
            # against the budget, so the chunk also under-fills. Reproduced exactly,
            # because a control has to be the real thing.
            block = block[:max_chars]
        current.append(block)
        length += raw_len + 2
    if current:
        packed.append(current)
    return packed


def chunk_page(
    page: DocPage,
    max_chars: int = 800,
    *,
    sections: bool = False,
    oversize: Oversize = "keep",
) -> list[DocChunk]:
    """Slice one page into rankable chunks.

    ``sections=False`` packs paragraphs to the budget (the course's shape).
    ``sections=True`` starts a new chunk at every markdown heading first, then packs
    within the section, and carries the heading stack on each chunk — so a slice from
    the middle of a page still says which section it came from.
    """
    blocks = _blocks(page.text)
    chunks: list[DocChunk] = []

    def emit(groups: list[list[str]], heading_path: tuple[str, ...]) -> None:
        for group in groups:
            text = "\n\n".join(group).strip()
            if text:
                chunks.append(DocChunk(page.page_id, len(chunks), text, heading_path))

    if not sections:
        emit(_pack([b for b, _ in blocks], max_chars, oversize), ())
        return chunks

    stack: list[str] = []
    pending: list[str] = []
    path: tuple[str, ...] = ()
    for block, heading in blocks:
        if heading is None:
            pending.append(block)
            continue
        if pending:
            emit(_pack(pending, max_chars, oversize), path)
            pending = []
        level, shown = heading
        del stack[level - 1 :]
        stack.append(shown.strip())
        path = tuple(stack)
        pending.append(block)
    if pending:
        emit(_pack(pending, max_chars, oversize), path)
    return chunks


# --------------------------------------------------------------------------
# The projection, and the index
# --------------------------------------------------------------------------


def unit_for_chunk(page: DocPage, chunk: DocChunk) -> RankableUnit:
    """One chunk as the scorer sees it.

    ``title`` carries the page title and, when the section chunker ran, the heading
    stack — so the structure a writer already put in the document becomes ranking
    evidence through the SHIPPED double-count instead of through a new weight nobody
    fitted. ``identity`` is the page id, which is the document's analogue of an
    operationId: an identifier whose sub-words are exactly what a question that means
    this page overlaps.
    """
    title = page.title
    if chunk.heading_path:
        title = " > ".join((page.title, *chunk.heading_path))
    return RankableUnit(
        unit_id=chunk.chunk_id,
        title=title,
        body=chunk.text,
        locator=chunk.chunk_id,
        identity=page.page_id,
        tags=page.tags,
    )


@dataclass(frozen=True)
class DocHit:
    """A retrieved passage, with the page it came from and the score that chose it."""

    chunk: DocChunk
    page: DocPage
    score: int

    @property
    def page_id(self) -> str:
        return self.chunk.page_id


class DocIndex:
    """Rank a document corpus with the lexical arm the API surface ships.

    Build once, query many times: the folded surfaces are computed at construction,
    because a document corpus is static between builds and re-folding 879 chunks per
    query is the difference between an eval you run and one you do not. (``Catalog``
    deliberately does the opposite — its tokenizer is a swappable module global.)
    """

    def __init__(
        self,
        pages: Sequence[DocPage],
        *,
        max_chars: int = 800,
        sections: bool = False,
        oversize: Oversize = "keep",
        tokenize: Tokenize | None = None,
    ):
        if tokenize is None:
            # Read the module attribute, not the name: `catalog._tokens` is what the
            # tokenizer arms swap at runtime, and importing the name would bind one
            # vocabulary here while the rest of the engine used another.
            from . import catalog

            tokenize = catalog._tokens
        self.pages: dict[str, DocPage] = {p.page_id: p for p in pages}
        self.chunks: list[DocChunk] = [
            chunk
            for page in pages
            for chunk in chunk_page(
                page, max_chars, sections=sections, oversize=oversize
            )
        ]
        self._folded: list[FoldedUnit] = [
            fold_unit(unit_for_chunk(self.pages[c.page_id], c), tokenize)
            for c in self.chunks
        ]
        self._tokenize = tokenize

    def search_scored(
        self,
        query: str,
        limit: int = 3,
        *,
        one_per_source: bool = True,
        gate: bool = False,
    ) -> list[DocHit]:
        """The passages that answer ``query``, best first. EMPTY IS AN ANSWER.

        ``one_per_source`` is a POST-RANK policy, applied here beside where the API
        path applies its auth filter — never inside the scorer. It is worth measuring
        on its own (the course measured the same rule at +4 points alone) and a rule
        folded into a score cannot be switched off to find that out.

        ``gate`` is the API surface's intent gate, off by default and exposed only so
        the report can show what turning it on costs a document corpus. It is not a
        tuning knob: leaving it on is the shape of "the page must say in its heading
        what it teaches in its body", which is not how prose works.
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []
        ranked = rank_units(
            self._folded,
            query_tokens,
            # Rank the whole corpus, then let the post-rank policy cut it: truncating
            # first would let one long page eat every slot before dedupe could see it.
            limit=len(self._folded),
            gate=gate,
            fallback=False,
        )
        hits: list[DocHit] = []
        seen: set[str] = set()
        for item in ranked:
            chunk = self.chunks[item.index]
            if one_per_source:
                if chunk.page_id in seen:
                    continue
                seen.add(chunk.page_id)
            hits.append(DocHit(chunk, self.pages[chunk.page_id], item.score))
            if len(hits) >= limit:
                break
        return hits
