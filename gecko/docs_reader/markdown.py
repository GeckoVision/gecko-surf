"""Markdown → ``ParsedPage`` — the ``.md`` twin path for ``gecko from-docs``.

A growing class of docs (Stripe, anything on Mintlify) publishes a **``.md`` twin** of
every page — the same URL with ``.md`` appended returns authored markdown instead of
hydrated HTML. That twin is *cheaper than a browser render and higher-signal than a
scraped DOM*: it is the text the provider meant an agent to read.

This module is the markdown equivalent of :mod:`gecko.docs_reader.html`: it turns a
markdown document into the SAME ``ParsedPage`` node stream the pure parser consumes
(``heading`` / ``code`` / ``table`` in document order), so nothing downstream changes.
Stdlib-only, deterministic, no markdown dependency — a small block-level scanner, because
the parser only needs three block kinds, not a full CommonMark tree.

Untrusted input, same as HTML: the produced nodes flow through the same parser, sanitizer,
and quarantine path. Markdown here changes where the bytes came from, not how far we trust
them.
"""

from __future__ import annotations

import re

from .models import PageNode, ParsedPage, Table

__all__ = ["page_from_markdown"]

#: ATX headings: 1-6 leading '#'. We keep the level out of the node (the parser only
#: needs the text + document order), matching how html.py flattens h1..h6.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
#: A fenced code block opener/closer: ``` or ~~~ (3+), optional info string.
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
#: A table row is a line containing an unescaped pipe.
_TABLE_ROW = re.compile(r"^\s*\|?(.+\|.+?)\|?\s*$")
#: The header/body separator row: cells of only -, :, and spaces.
_TABLE_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")


def _split_row(line: str) -> list[str]:
    """Split a pipe-table row into trimmed cells, honoring ``\\|`` escapes."""
    # protect escaped pipes, split on the rest, then restore.
    parts = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [p.replace("\\|", "|").strip() for p in parts]


def page_from_markdown(url: str, md_text: str) -> ParsedPage:
    """Parse markdown into the ordered heading/code/table node stream.

    Document order is preserved (load-bearing: a param table belongs to the code block
    that follows it, under the heading that precedes it). Fenced code is captured verbatim
    — code samples are the highest-signal source of the real route + auth header. A
    GitHub-Flavored pipe table (header row + ``|---|`` separator) becomes a ``Table`` node.
    """
    lines = (md_text or "").splitlines()
    nodes: list[PageNode] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(2)[0]  # ` or ~
            body: list[str] = []
            i += 1
            # consume to the matching closing fence (same marker char), or EOF.
            while i < n:
                close = _FENCE.match(lines[i])
                if close and close.group(2)[0] == marker:
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            nodes.append(PageNode(kind="code", text="\n".join(body)))
            continue

        heading = _HEADING.match(line)
        if heading:
            nodes.append(PageNode(kind="heading", text=heading.group(2).strip()))
            i += 1
            continue

        # a pipe table: a row line immediately followed by a separator row.
        if (
            _TABLE_ROW.match(line)
            and i + 1 < n
            and _TABLE_SEP.match(lines[i + 1])
            and "|" in line
        ):
            headers = _split_row(line)
            rows: list[list[str]] = []
            i += 2  # skip header + separator
            while i < n and _TABLE_ROW.match(lines[i]) and "|" in lines[i]:
                if _TABLE_SEP.match(
                    lines[i]
                ):  # a stray second separator ends the table
                    break
                rows.append(_split_row(lines[i]))
                i += 1
            nodes.append(
                PageNode(kind="table", table=Table(headers=headers, rows=rows))
            )
            continue

        i += 1

    return ParsedPage(url=url, nodes=nodes)
