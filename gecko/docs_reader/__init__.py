"""docs_reader — turn a human doc page into a draft OpenAPI the engine can comprehend.

Productized from ``spikes/docs_reader``: the pure OFFLINE core (models + parser +
emit) plus a stdlib HTML rendering seam and the ``from_docs`` orchestration. Import
stays dep-light — stdlib + existing engine modules only. The agent-browser live
driver stays optional in ``spikes/docs_reader`` for JS-rendered nav.

    from gecko.docs_reader import from_docs
    draft = from_docs("https://docs.example.com/api").draft
"""

from __future__ import annotations

from .core import DocsDraft, count_review_flags, from_docs
from .emit import build_draft_openapi
from .html import page_from_html
from .models import CandidateOp, CandidateParam, ParsedPage
from .parser import detect_uuid_auth, parse_page, parsed_page_from_nodes

__all__ = [
    "DocsDraft",
    "from_docs",
    "count_review_flags",
    "build_draft_openapi",
    "page_from_html",
    "parse_page",
    "parsed_page_from_nodes",
    "detect_uuid_auth",
    "CandidateOp",
    "CandidateParam",
    "ParsedPage",
]
