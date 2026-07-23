"""The ``.md`` twin path in from-docs — offline (injected fetch, no network).

Why it exists: Stripe/Mintlify publish a ``.md`` twin of every docs page (``<url>.md``)
— authored markdown that is cheaper than a browser render and higher-signal than a
scraped DOM. The fetch chain now tries it BETWEEN the static parse and the browser
render. This module covers the markdown→node parser and the chain ordering.
"""

from __future__ import annotations

from gecko.docs_reader import core
from gecko.docs_reader.core import _md_sibling
from gecko.docs_reader.markdown import page_from_markdown
from gecko.docs_reader.parser import parse_page

# A markdown doc the parser can turn into an operation (heading names it, table gives a
# param, fenced curl gives the route + host).
MD_DOC = """\
# Widgets API

Base URL: `https://api.example.com`

## POST /v1/widgets

Create a widget.

| Name | Type | Required | Description |
| ---- | ---- | -------- | ----------- |
| name | string | yes | The widget name |

```bash
curl https://api.example.com/v1/widgets -X POST -d '{"name": "x"}'
```
"""

SHELL_HTML = (
    '<html><body><div id="root"></div><script src="/a.js"></script></body></html>'
)


# --- the markdown parser produces the same node stream as html --------------------


def test_markdown_headings_code_and_tables_become_nodes() -> None:
    page = page_from_markdown("x", MD_DOC)
    kinds = [n.kind for n in page.nodes]
    assert "heading" in kinds and "code" in kinds and "table" in kinds
    # document order: the table sits before the code block, under a heading (the
    # association the parser depends on).
    t = kinds.index("table")
    assert kinds[t + 1] == "code"


def test_fenced_code_is_captured_verbatim() -> None:
    page = page_from_markdown("x", MD_DOC)
    code = next(n.text for n in page.nodes if n.kind == "code")
    assert "curl https://api.example.com/v1/widgets -X POST" in code


def test_a_pipe_table_becomes_header_plus_rows() -> None:
    page = page_from_markdown("x", MD_DOC)
    table = next(n.table for n in page.nodes if n.kind == "table")
    assert table is not None
    assert table.headers == ["Name", "Type", "Required", "Description"]
    assert table.rows == [["name", "string", "yes", "The widget name"]]


def test_a_row_without_a_separator_is_not_a_table() -> None:
    """A lone pipe line (e.g. prose with a pipe) must not be mistaken for a table."""
    page = page_from_markdown("x", "a | b was mentioned in passing\n\nnext line")
    assert not any(n.kind == "table" for n in page.nodes)


def test_tilde_fences_and_escaped_pipes() -> None:
    md = "~~~\ncode\n~~~\n\n| a \\| b | c |\n| --- | --- |\n| x \\| y | z |\n"
    page = page_from_markdown("x", md)
    assert any(n.kind == "code" and n.text == "code" for n in page.nodes)
    table = next(n.table for n in page.nodes if n.kind == "table")
    assert table.headers == ["a | b", "c"]
    assert table.rows == [["x | y", "z"]]


def test_the_markdown_doc_parses_into_an_operation() -> None:
    ops = parse_page(page_from_markdown("https://api.example.com/docs", MD_DOC))
    assert any(o.http_method == "POST" and "/v1/widgets" in o.http_path for o in ops)


# --- the .md sibling URL ---------------------------------------------------------


def test_md_sibling_appends_dot_md() -> None:
    assert (
        _md_sibling("https://docs.example.com/api") == "https://docs.example.com/api.md"
    )
    assert (
        _md_sibling("https://docs.example.com/api/")
        == "https://docs.example.com/api.md"
    )
    assert (
        _md_sibling("https://docs.example.com/api?x=1")
        == "https://docs.example.com/api.md?x=1"
    )


# --- the fetch chain: static -> .md twin -> render -------------------------------


def _fake_fetch(mapping: dict[str, str]):
    """A _fetch stand-in: return mapped text for a URL, else raise (a 404-shaped miss)."""

    def fetch(source: str, resolver=None) -> str:
        if source in mapping:
            return mapping[source]
        raise OSError("not found")

    return fetch


def test_the_md_twin_is_tried_when_static_recovers_little(monkeypatch) -> None:
    url = "https://docs.example.com/api"
    monkeypatch.setattr(
        core, "_fetch", _fake_fetch({url: SHELL_HTML, f"{url}.md": MD_DOC})
    )
    draft = core.from_docs(url, render=False)
    assert draft.from_md is True
    assert draft.rendered is False
    assert len(draft.ops) >= 1  # recovered from the .md twin, no browser


def test_a_md_url_is_parsed_as_markdown_directly(monkeypatch) -> None:
    url = "https://docs.example.com/api.md"
    monkeypatch.setattr(core, "_fetch", _fake_fetch({url: MD_DOC}))
    draft = core.from_docs(url, render=False)
    assert draft.from_md is True
    assert len(draft.ops) >= 1


def test_no_md_twin_falls_through_and_is_explained(monkeypatch) -> None:
    """A 404 on the twin must not fail the run; it records why and falls through."""
    url = "https://docs.example.com/api"
    monkeypatch.setattr(core, "_fetch", _fake_fetch({url: SHELL_HTML}))  # no .md entry
    draft = core.from_docs(url, render=False)
    assert draft.from_md is False
    assert any(".md twin unavailable" in w for w in draft.warnings)


def test_the_md_twin_is_not_tried_when_static_already_worked(monkeypatch) -> None:
    """The twin is a fallback — a page that already parsed must not fetch it (one GET
    saved, and the static result stands)."""
    url = "https://docs.example.com/api"
    # Two ops so the static parse is clearly >= _RENDER_BELOW_OPS ("already worked").
    html_that_parses = (
        "<h2>POST /v1/widgets</h2>"
        "<pre>curl https://api.example.com/v1/widgets -X POST</pre>"
        "<h2>GET /v1/widgets/list</h2>"
        "<pre>curl https://api.example.com/v1/widgets/list</pre>"
    )
    fetched: list[str] = []

    def fetch(source: str, resolver=None) -> str:
        fetched.append(source)
        return html_that_parses

    monkeypatch.setattr(core, "_fetch", fetch)
    draft = core.from_docs(url, render=False)
    assert draft.from_md is False
    assert fetched == [url]  # the .md twin was never fetched
    assert len(draft.ops) >= 1


def test_the_md_twin_is_preferred_over_the_browser_render(monkeypatch) -> None:
    """Ordering: the cheap .md twin is tried BEFORE the expensive browser render, so a
    successful twin means the renderer is never called."""
    url = "https://docs.example.com/api"
    monkeypatch.setattr(
        core, "_fetch", _fake_fetch({url: SHELL_HTML, f"{url}.md": MD_DOC})
    )
    rendered_called: list[str] = []

    def renderer(u: str) -> str:
        rendered_called.append(u)
        return SHELL_HTML

    draft = core.from_docs(url, render=renderer)
    assert draft.from_md is True
    assert rendered_called == []  # render skipped — the twin already succeeded
