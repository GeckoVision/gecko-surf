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

from ..netguard import Resolver, safe_get
from .emit import build_draft_openapi
from .html import page_from_html
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
    text = _fetch(source, resolver=resolver)
    page = page_from_html(source, text)
    ops = parse_page(page)

    # JS-hydrated docs return a shell to a stdlib fetch, so the parser finds nothing.
    # Retry through a real browser — decided on RECOVERED OPS rather than a guess about
    # the HTML, because "did we actually learn anything" is the question that matters.
    # Never for a local path (nothing to hydrate) and never when no renderer exists.
    rendered = False
    warnings: list[str] = []
    if (
        len(ops) < _RENDER_BELOW_OPS
        and source.startswith(("http://", "https://"))
        and render is not False
    ):
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
    )
