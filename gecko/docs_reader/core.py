"""from-docs orchestration — human doc page -> draft OpenAPI, the whole $0 flow.

Ties the offline core together: SSRF-safe fetch (or a local dev path) -> stdlib HTML
node stream -> pure ``parser`` -> ``emit`` -> a draft OpenAPI the unmodified engine
can comprehend. Nothing here touches a browser; agent-browser stays an optional,
better renderer behind ``spikes/docs_reader`` for JS-heavy nav.

Control plane: we fetch the doc *surface* only and never persist the bytes — the
draft is derived and returned, the source text is discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..netguard import Resolver, UnsafeUrlError, safe_get
from .emit import build_draft_openapi
from .html import page_from_html
from .markdown import page_from_markdown
from .models import CandidateOp
from .parser import detect_uuid_auth, parse_page
from .render import RenderError, Renderer, default_renderer

#: Below this many recovered operations we assume the page never hydrated and retry with
#: a rendered fetch. A real single-endpoint page recovers 1-2 ops; a JS-only shell
#: recovers 0, which is what both Privy and Birdeye did.
_RENDER_BELOW_OPS = 2

_DEFAULT_TITLE = "Recovered API (draft, docs_reader)"


@dataclass
class DocsDraft:
    """The result of a ``from_docs`` run — a draft spec plus honest review metadata."""

    draft: dict[str, Any]
    ops: list[CandidateOp]
    source: str
    uuid_auth: dict[str, str] | None = None
    review_notes: int = 0  # count of x-review annotations a human must confirm
    low_confidence: int = 0  # count of low/medium x-draft-confidence markers
    title: str = _DEFAULT_TITLE
    warnings: list[str] = field(default_factory=list)
    #: True when the operations came from a BROWSER-RENDERED page rather than a static
    #: fetch. Still untrusted input — this records provenance for a reviewer, not trust.
    rendered: bool = False
    #: True when the operations came from the ``.md`` twin (authored markdown) rather
    #: than HTML. Provenance for a reviewer; markdown is untrusted like any doc source.
    from_md: bool = False


def count_review_flags(draft: dict[str, Any]) -> tuple[int, int]:
    """Walk a draft and count (x-review notes, low/medium-confidence markers).

    These are the honesty signals ``from-docs`` surfaces: exactly what a human must
    confirm before trusting the draft.
    """
    notes = 0
    low = 0

    def walk(node: Any) -> None:
        nonlocal notes, low
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "x-review":
                    notes += 1
                elif key == "x-draft-confidence" and value in ("low", "medium"):
                    low += 1
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(draft)
    return notes, low


def _fetch(source: str, *, resolver: Resolver | None = None) -> str:
    """Return the doc text. http(s) is SSRF-validated + capped; a path is dev-only."""
    if source.startswith(("http://", "https://")):
        # safe_get validates the URL (and every redirect hop) before reading.
        return safe_get(source, resolver=resolver)
    return Path(source).read_text(encoding="utf-8")


def _md_sibling(url: str) -> str:
    """The ``.md`` twin of a docs URL: append ``.md`` to the path, preserving any query
    string. ``/api`` -> ``/api.md``; ``/api/`` -> ``/api.md``; a query is kept."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(parts._replace(path=f"{path}.md"))


def _title_for(explicit: str | None, page_url: str, first_heading: str) -> str:
    if explicit:
        return explicit
    if first_heading:
        return f"{first_heading} (draft, docs_reader)"
    return _DEFAULT_TITLE


def from_docs(
    source: str,
    *,
    title: str | None = None,
    resolver: Resolver | None = None,
    render: Renderer | bool | None = None,
) -> DocsDraft:
    """Recover a draft OpenAPI from a doc page (URL or local HTML path).

    Deterministic and offline for a static page: fetch -> HTML node stream -> parse
    -> emit. The returned ``DocsDraft.draft`` loads unchanged in ``AgentApiClient``.

    ``render`` controls the JS fallback (:mod:`gecko.docs_reader.render`), used only when
    the static fetch recovers almost nothing:

    * ``None``/``True`` — use ``agent-browser`` if it is installed (the default; absent
      browser simply means no fallback, never an error)
    * ``False`` — never render (hermetic tests, air-gapped runs)
    * a callable — an injected renderer (Pattern B: the fallback is falsifiable offline)
    """
    is_url = source.startswith(("http://", "https://"))
    rendered = False
    from_md = False
    warnings: list[str] = []

    text = _fetch(source, resolver=resolver)
    # A ``.md`` URL is markdown, not HTML — parse it as such (Stripe/Mintlify serve a
    # ``.md`` twin of every page). Otherwise parse HTML.
    if source.endswith(".md"):
        page = page_from_markdown(source, text)
        from_md = True
    else:
        page = page_from_html(source, text)
    ops = parse_page(page)

    # Cheap high-signal fallback BEFORE the browser: try the ``<url>.md`` twin. Authored
    # markdown beats a scraped/hydrated DOM and costs one GET, no browser. Only when the
    # static parse recovered almost nothing and we are not already on a ``.md`` URL.
    if len(ops) < _RENDER_BELOW_OPS and is_url and not source.endswith(".md"):
        md_url = _md_sibling(source)
        try:
            md_text = _fetch(md_url, resolver=resolver)
            md_page = page_from_markdown(md_url, md_text)
            md_ops = parse_page(md_page)
            if len(md_ops) > len(ops):
                page, ops, from_md = md_page, md_ops, True
        except (OSError, UnsafeUrlError) as exc:
            # No ``.md`` twin (404), or an unsafe/invalid sibling URL — fall through to
            # the render path; record why so a 0-op answer stays explainable.
            warnings.append(f".md twin unavailable: {type(exc).__name__}")

    # JS-hydrated docs return a shell to a stdlib fetch, so the parser finds nothing.
    # Retry through a real browser — decided on RECOVERED OPS rather than a guess about
    # the HTML, because "did we actually learn anything" is the question that matters.
    # Never for a local path (nothing to hydrate) and never when no renderer exists.
    if len(ops) < _RENDER_BELOW_OPS and is_url and not from_md and render is not False:
        renderer: Renderer | None = (
            default_renderer() if render is None or render is True else render
        )
        if renderer is not None:
            try:
                text = renderer(source)
                page = page_from_html(source, text)
                ops = parse_page(page)
                rendered = True
            except RenderError as exc:
                # A failed render must never fail the run — we still have the static
                # result. Surface the reason so a 0-op answer is explainable.
                warnings.append(f"render fallback failed: {exc}")

    uuid_auth = detect_uuid_auth([page])

    first_heading = next((n.text for n in page.nodes if n.kind == "heading"), "")
    doc_title = _title_for(title, source, first_heading)
    draft = build_draft_openapi(
        ops, title=doc_title, source_urls=[source], uuid_auth=uuid_auth
    )
    notes, low = count_review_flags(draft)
    return DocsDraft(
        draft=draft,
        ops=ops,
        source=source,
        uuid_auth=uuid_auth,
        review_notes=notes,
        low_confidence=low,
        title=doc_title,
        warnings=warnings,
        rendered=rendered,
        from_md=from_md,
    )
