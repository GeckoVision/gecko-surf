"""Legal terms, versioned by hash: what the pages say, without trusting what they say.

A page is fetched, its visible text normalized (scripts, styles and tags dropped,
entities decoded, NFKC, whitespace collapsed; the ``<main>`` or ``<article>`` body when
there is one, so a changed menu or footer does not read as changed terms), and hashed.
The hash is the value compared run to run. Up to three sentences carrying a legal key
term are kept — sanitized, capped — for the FILE only; a tool result never carries them.

A page that carries none of the key terms is treated as unread, not as a version: a
script shell or a moved page hashes the same every time and would otherwise report
"unchanged" for terms it never showed.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass

from ..netguard import UnsafeUrlError
from ..orthogonal.steps import PublicStep
from ..orthogonal.supplier.text import untrusted_summary
from .wire import PublicFetch
from .xstocks import LegalPage

KEY_TERMS = (
    "legal claim",
    "voting rights",
    "not equivalent",
    "underlying",
    "liquidation",
    "custod",
    "collateral",
    "backed assets",
)
MAX_SENTENCES = 3
SENTENCE_CAP = 240
_DROP = re.compile(
    r"<(script|style|noscript|svg|template|head|nav|footer)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_MAIN = re.compile(r"<(main|article)\b[^>]*>(.*)</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def page_text(markup: str) -> str:
    body = _DROP.sub(" ", markup)
    main = _MAIN.search(body)
    if main is not None:
        body = main.group(2)
    text = html.unescape(_TAG.sub(" ", body))
    text = unicodedata.normalize("NFKC", text)
    return _SPACE.sub(" ", text).strip()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key_sentences(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if any(term in sentence.lower() for term in KEY_TERMS):
            clean = untrusted_summary(sentence, SENTENCE_CAP)
            if clean and clean not in out:
                out.append(clean)
        if len(out) == MAX_SENTENCES:
            break
    return tuple(out)


@dataclass(frozen=True)
class LegalRead:
    page: LegalPage
    sha256: str | None
    sentences: tuple[str, ...]
    chars: int
    step: PublicStep
    reason: str = ""


def read_legal(page: LegalPage, fetch: PublicFetch) -> LegalRead:
    try:
        status, markup = fetch(page.url)
    except UnsafeUrlError:
        step = PublicStep(page.field, ("url",), None, (), 0, "refused")
        return LegalRead(page, None, (), 0, step, "the network guard refused the URL")
    except (OSError, UnicodeDecodeError):
        step = PublicStep(page.field, ("url",), None, (), 0, "unreachable")
        return LegalRead(page, None, (), 0, step, "the page could not be fetched")
    if not 200 <= status < 300:
        step = PublicStep(page.field, ("url",), status, (), 0, "upstream_error")
        return LegalRead(page, None, (), 0, step, f"the page answered HTTP {status}")
    text = page_text(markup)
    sentences = key_sentences(text)
    step = PublicStep(page.field, ("url",), status, ("text",), len(sentences))
    if not sentences:
        return LegalRead(
            page,
            None,
            (),
            len(text),
            step,
            "the page carries none of the expected legal terms (moved, or a script "
            "shell), so it is not stored as a version",
        )
    return LegalRead(page, text_sha256(text), sentences, len(text), step)
