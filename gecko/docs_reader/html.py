"""Stdlib HTML -> the parser's ordered node stream (the $0 rendering seam).

The spike's live driver captured an ordered ``h1..h4 / pre / table`` stream via
agent-browser's ``querySelectorAll`` (which returns DOM order). This module does the
same extraction from *static* HTML with only ``html.parser`` — no browser, no
network — so ``from-docs`` works offline on any server-rendered doc page. It is the
swappable rendering seam: the pure ``parser`` never learns where its nodes came from,
so agent-browser (for JS-rendered nav) and this stdlib reader are interchangeable.

Document order is the load-bearing signal (a param table belongs to the curl that
follows it), so we emit nodes in encounter order. All HTML is untrusted: we cap the
input, never eval, and collapse only whitespace we don't need.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from .models import ParsedPage
from .parser import parsed_page_from_nodes

_HEADINGS = {"h1", "h2", "h3", "h4"}
_MAX_HTML_CHARS = 5_000_000  # defensive cap on an untrusted document


def _collapse(text: str) -> str:
    return " ".join(text.split())


class _NodeExtractor(HTMLParser):
    """Emit ordered heading/code/table node dicts, mirroring the driver's stream.

    Only the structural tags (``h1..h4``, ``pre``, ``table``/``tr``/``th``/``td``)
    drive state; inline tags nested inside them (``<code>``, ``<span>``, ``<a>``) are
    ignored so their text still flows into the enclosing block.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict[str, Any]] = []
        # Heading / <pre> capture (mutually exclusive with cell capture).
        self._block_kind: str | None = None  # "heading" | "code"
        self._block_buf: list[str] = []
        # Table capture.
        self._table_depth = 0
        self._headers: list[str] = []
        self._rows: list[list[str]] = []
        self._row_cells: list[str] = []
        self._row_has_td = False
        self._cell_target: str | None = None  # "th" | "td"
        self._cell_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        t = tag.lower()
        if t in _HEADINGS or t == "pre":
            # Start a block only if not already inside one (ignore nesting).
            if self._block_kind is None and self._cell_target is None:
                self._block_kind = "heading" if t in _HEADINGS else "code"
                self._block_buf = []
        elif t == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._headers = []
                self._rows = []
        elif t == "tr" and self._table_depth:
            self._row_cells = []
            self._row_has_td = False
        elif t in ("th", "td") and self._table_depth:
            self._cell_target = t
            self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if (t in _HEADINGS or t == "pre") and self._block_kind is not None:
            text = "".join(self._block_buf)
            self.nodes.append(
                {
                    "kind": self._block_kind,
                    "text": _collapse(text) if self._block_kind == "heading" else text,
                }
            )
            self._block_kind = None
            self._block_buf = []
        elif t == "th" and self._cell_target == "th":
            self._headers.append(_collapse("".join(self._cell_buf)))
            self._cell_target = None
        elif t == "td" and self._cell_target == "td":
            self._row_cells.append(_collapse("".join(self._cell_buf)))
            self._row_has_td = True
            self._cell_target = None
        elif t == "tr" and self._table_depth and self._row_has_td:
            self._rows.append(self._row_cells)
            self._row_cells = []
            self._row_has_td = False
        elif t == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0:
                self.nodes.append(
                    {"kind": "table", "headers": self._headers, "rows": self._rows}
                )

    def handle_data(self, data: str) -> None:
        if self._cell_target is not None:
            self._cell_buf.append(data)
        elif self._block_kind is not None:
            self._block_buf.append(data)


def page_from_html(url: str, html_text: str) -> ParsedPage:
    """Extract the parser's ordered node stream from a static HTML document."""
    extractor = _NodeExtractor()
    extractor.feed(html_text[:_MAX_HTML_CHARS])
    extractor.close()
    return parsed_page_from_nodes(url, extractor.nodes)
