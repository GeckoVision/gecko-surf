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
) -> DocsDraft:
    """Recover a draft OpenAPI from a doc page (URL or local HTML path).

    Deterministic and offline for a static page: fetch -> HTML node stream -> parse
    -> emit. The returned ``DocsDraft.draft`` loads unchanged in ``AgentApiClient``.
    """
    text = _fetch(source, resolver=resolver)
    page = page_from_html(source, text)
    ops = parse_page(page)
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
    )
