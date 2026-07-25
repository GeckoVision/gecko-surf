"""Skill Guard comprehension seam — scan an untrusted doc page during ``from-docs``.

This is where the three-layer image/convention defense meets comprehension. When
``from_docs`` recovers a draft from a human doc page, that page is untrusted input:
it may be a GhostCommit-style delivery file (a convention that tells the agent to OCR
an embedded diagram and exfiltrate ``.env``) or it may inline the payload image itself.

Two untrusted channels are scanned with Gecko's EXISTING engines — no new classifier:

* **L1 — page text:** :func:`gecko.sanitize.scan_convention_text` catches the delivery
  (the follow-rendered-content + numeric-encode-exfil combination).
* **Embedded images:** every inline ``data:image/...;base64,`` URI is decoded (under a
  pre-decode size cap) and run through :func:`gecko.imagescan.scan_image` (L2 metadata /
  trailing bytes, plus L3 OCR when the ``[ocr]`` extra is present).

The verdict's basis (channel + rule names only) is stamped by ``from_docs`` onto the
draft's ``info`` so the born-quarantined surface *names why* it is untrusted, flowing to
:func:`gecko.surfaces.spec_is_quarantined` through the same fail-closed marker seam.

**NAMED RESIDUAL — remote images are NOT fetched.** Only inline ``data:`` URIs are
scanned (zero network, no SSRF surface). A remote ``![](https://…)`` image is deliberately
left unfetched in this seam: :func:`gecko.netguard.safe_get` is text-oriented (it decodes
the body to ``str``), so scanning a binary image would need a new bytes-fetch path — extra
network + decode risk on the comprehension path for marginal value, since the flagship PoC
inlines the image. If remote image scanning is added later it MUST go through
``netguard.validate_public_url`` (no private/loopback/link-local, http(s) only) with a byte
cap, and any fetch failure must be swallowed so comprehension never crashes.

Control plane (invariant #1): the basis carries channel/rule names and byte-counts only —
never the decoded image bytes, the payload text, or a secret value. Nothing is persisted.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from gecko import sanitize
from gecko.imagescan import scan_image

__all__ = ["PageScanVerdict", "scan_doc_page"]

#: Cap the base64 payload decoded from a SINGLE ``data:`` URI, applied BEFORE decoding.
#: A data URI is attacker-sized and an unbounded decode would OOM. 12 MiB of base64
#: decodes to ~9 MiB — orders above any real embedded diagram, well below a bomb.
_MAX_DATA_URI_B64 = 12 << 20

#: Inline image data URIs only: ``data:image/<subtype>;base64,<payload>`` as they appear
#: in markdown ``![](…)`` or HTML ``<img src=…>``. A remote ``http(s)`` src is ignored
#: here by construction (see the module docstring's named residual).
_DATA_IMAGE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\s]+)")


@dataclass(frozen=True)
class PageScanVerdict:
    """Why a comprehended page is (or is not) untrusted — channel + rule names only.

    ``poison_basis`` quarantines the surface with a visible reason; ``review_basis`` is a
    softer note (a structural image anomaly with no injection hit). Empty both == clean.
    """

    poison_basis: tuple[str, ...]
    review_basis: tuple[str, ...]

    @property
    def poisoned(self) -> bool:
        return bool(self.poison_basis)


def _iter_data_images(text: str) -> Iterator[bytes]:
    """Yield the decoded bytes of each inline ``data:image`` URI in ``text``.

    Capped before decode (bomb guard); an undecodable base64 run is skipped, never raised
    — a malformed embedded image must not crash comprehension.
    """
    for match in _DATA_IMAGE.finditer(text):
        b64 = match.group(1)[:_MAX_DATA_URI_B64]
        compact = re.sub(r"\s+", "", b64)  # the regex tolerates wrapped/spaced base64
        try:
            yield base64.b64decode(compact, validate=False)
        except (binascii.Error, ValueError):
            continue


def scan_doc_page(
    text: str, *, image_ocr: Callable[[bytes], str] | None = None
) -> PageScanVerdict:
    """Scan one untrusted doc page's text + inline images. Never raises.

    ``image_ocr`` is the injectable OCR seam threaded to :func:`imagescan.scan_image` — a
    fake for offline, tesseract-free tests of the OCR-text → verdict path; ``None`` uses
    the real (opt-in, no-op when the extra is absent) OCR.
    """
    poison: list[str] = []
    review: list[str] = []

    # L1: the delivery file. Convention-text tells fold into the poison basis directly.
    poison.extend(
        f"convention-text → {rule}" for rule in sanitize.scan_convention_text(text)
    )

    # Embedded images: decode inline data: URIs and run the image scanner (L2 + L3).
    for data in _iter_data_images(text):
        try:
            verdict = (
                scan_image(data, ocr=image_ocr)
                if image_ocr is not None
                else scan_image(data)
            )
        except Exception:
            # scan_image is contracted never to raise, but an untrusted-input scanner
            # must fail CLOSED: a scan that BREAKS on attacker bytes (e.g. a residual
            # decoder crash) is an anomaly, not a pass. Flag the page for human review
            # instead of silently continuing to a clean verdict. Name only, no payload.
            review.append("image:scan-error")
            continue
        if verdict.tier == "poison":
            poison.extend(f"image:{b}" for b in verdict.basis)
        elif verdict.tier == "review":
            review.extend(f"image:{b}" for b in verdict.basis)

    return PageScanVerdict(tuple(poison), tuple(review))
