"""Channel-fidelity normalisation for OCR-recovered text (L3).

The problem this solves
-----------------------
``gecko.sanitize`` is fed by two channels with different fidelity. Spec text arrives as
the author wrote it. OCR text arrives as the RENDERER left it. Two artifacts of the
pixel channel change what the scanner sees:

1. **Soft wraps.** A sentence the attacker wrote on one line comes back split across
   several. The sanitizer treats ``\\n`` as a hard sentence terminator — every
   ``[^.\\n]``-scoped gap and every ``_ordered_in_sentence`` check stops there. The
   attacker picks the image width, so on this channel the ATTACKER picks where the
   breaks fall. A single break placed inside a signature was a one-character bypass of
   ``secret_exfil``, the ``forget`` arm of ``prompt_injection``, and ``fund_routing``
   branch (a).

2. **Shattered address runs.** Tesseract inserts spaces inside a long base58 literal:
   ``9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin`` comes back as three space-separated
   runs, none of which satisfies ``_ADDR``. The pixel channel reproduces the SPACED-HEX
   evasion (``0x de ad be ef … remove the spaces``) accidentally, on every image that
   contains an address.

So this module undoes the renderer, and only the renderer. It changes no rule and
relaxes no threshold.

The invariant (this is the design, not a heuristic)
--------------------------------------------------
**Normalisation must reproduce the CLEAN-channel verdict — never exceed it, never fall
short of it.** The pixel channel should reach the same conclusion the same text would
have reached had it arrived as spec prose. That single statement decides every guard,
and it is stricter and more useful than "minimise false positives":

* Falling SHORT means the attacker gets a free bypass by choosing an image width. A
  candidate that refused to unwrap any line of pure identifiers looked safest and cost
  **18 of 68** attack/width pairs across PR #330's own ``fund_routing`` attack set —
  every short ``verb → to → <address>`` payload loses its address to the wrap at some
  width.
* EXCEEDING it means inventing findings. A candidate with no table guard fused three
  separate cells of a benign ID table into one 37-character pseudo-address and
  quarantined a wholly benign document — a hit the source text could never produce.

Measured, over 6,075 committed spec prose fields wrapped at 4 widths (24,300 variants)
plus the committed benign image fixtures: **zero** new false positives. Over PR #330's
17 ``fund_routing`` attacks at the same 4 widths: **37/68** pairs survive the renderer
raw, **68/68** after normalisation.

Scope: the OCR channel ONLY (``imagescan._ocr_hits``). Spec text is never normalised —
there the line structure is authored, not rendered, and rewriting it would be a change
to the rules rather than to the channel.

The guards
----------
* **Terminal punctuation.** A line ending in ``.!?:;`` is a finished clause; the next
  line starts a new one. This alone makes the common benign table caption
  (``Transfer the funds to the accounts:``) safe.
* **Blank line.** A blank line is a paragraph break and is never joined across.
* **Table rows.** A line of two or more cells, none of them a word, is a table — not
  prose continuation. One cell is a wrapped sentence and IS joined; see
  ``_is_table_row`` for why that threshold is load-bearing in both directions.
* **Word-shaped fragments are never fused.** Ordinary English is base58-legal:
  ``Requests are routed by path prefix`` fuses to a 29-character base58 run and would
  satisfy ``_ADDR``. A fragment therefore qualifies only if it is NOT word-shaped.

NAMED RESIDUALS (this is BEST-EFFORT defence-in-depth, not a closed channel)
---------------------------------------------------------------------------
Restoring fidelity closes the ACCIDENTAL gap — the one that fired on every image
containing an address, whether or not anyone was aiming at it. It is NOT a boundary
against an attacker who reads this file. Each guard is a seam, and each seam is reachable
on purpose: end the line in a period or colon, leave a blank line, split the address
across two lines, space its fragments by two spaces instead of one, or shatter it into
one-character runs. All of these were tried and all still evade
(``test_evasions_that_remain_open``). They are not closed because every available closure
trades a false positive in a channel we already label BEST-EFFORT, and a scanner people
mute protects nobody.

Also still open:

* OCR character substitution INSIDE an address (``Q`` read as ``O``). ``O`` is not in
  the base58 alphabet, so re-joining cannot help. Relaxing ``_ADDR`` to
  ``[A-Za-z0-9]{26,}`` would recover it and is refused — that class matches ordinary
  long identifiers and ``$ref`` paths, which is the false positive the base58 guard
  exists to prevent.
* Multi-column layouts. Row-major OCR already interleaves columns WITHIN a line, so
  this risk pre-dates unwrapping; unwrapping extends the interleave window ACROSS lines.
  Measured zero false positives on the committed benign fixtures, but it is the
  mechanism to watch if one ever appears.
* Encoded payloads. ``imagescan._decode_hits`` still reads the RAW recovery, because
  base64 is whitespace- and case-sensitive and un-wrapping it would corrupt more blobs
  than it repairs.
* Whatever PR #330's ``fund_routing`` rule already does on clean text, this channel now
  does too — including its false positives. A benign doc that puts an example address
  inside a routing sentence fires here exactly as it fires on spec prose. That is the
  invariant working as intended, not a new defect, but it is the shape to look at first
  if an operator reports an image-borne false positive.
"""

from __future__ import annotations

import re

__all__ = ["normalize_recovered_text", "unwrap_soft_breaks", "fuse_address_runs"]

#: Characters that end a clause. A line ending in one of these was not soft-wrapped.
_TERMINAL = ".!?:;"

#: Splits text into alternating non-space / space tokens, preserving the separators so
#: the fuse can rebuild the string exactly where it decides not to act.
_TOKEN = re.compile(r"\S+|\s+")

#: The base58 alphabet (no ``0``, ``O``, ``I``, ``l``) — the same class ``sanitize._ADDR``
#: uses. A fragment must be wholly inside it to be part of a shattered address.
_BASE58_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{2,}")

#: Shortest run ``sanitize._ADDR`` accepts. Fusing to anything shorter cannot change a
#: verdict, so the fuse declines and the text is left byte-identical.
_MIN_ADDRESS_LEN = 26

_TRIM = ".,;:!?()[]{}\"'`"


def _is_word_shaped(token: str) -> bool:
    """True if ``token`` reads as an ordinary word: alphabetic, no internal capital.

    ``the``, ``before``, ``Transfer`` are word-shaped. ``9xQeWvG8``, ``16bUx9EPjHmaT``
    and ``ZTbNMqUx`` are not. This is what keeps English prose out of the fuse and what
    tells a wrapped sentence apart from a table row.
    """
    core = token.strip(_TRIM)
    if not core or not core.isalpha():
        return False
    return not any(char.isupper() for char in core[1:])


def _is_address_fragment(token: str) -> bool:
    """True if ``token`` could be one piece of a whitespace-shattered address."""
    return bool(_BASE58_RUN.fullmatch(token)) and not _is_word_shaped(token)


def _is_table_row(line: str) -> bool:
    """True if ``line`` is a TABLE ROW: two or more cells, none of them a word.

    Such a line is not the continuation of the sentence above it, so unwrapping stops at
    it. Unwrapping a route-verb caption into an ID table, then fusing that row's cells,
    manufactured a 37-character pseudo-address inside a routing directive — a measured
    false positive on a wholly benign document (``benign_id_table``).

    The threshold is TWO cells, not one, and that is the load-bearing detail. A single
    address alone on a line is a WRAPPED SENTENCE, not a table, and blocking it cost 18
    of 68 attack/width pairs across PR #330's own attack set — every short
    ``verb → to → <address>`` payload loses its address to the wrap at some width. One
    cell is prose; two or more is a table.
    """
    tokens = line.split()
    return len(tokens) >= 2 and not any(_is_word_shaped(token) for token in tokens)


def unwrap_soft_breaks(text: str) -> str:
    """Rejoin lines the RENDERER split, leaving authored breaks intact.

    A break is treated as a soft wrap only when the preceding line ends mid-clause (no
    terminal punctuation), the following line is non-blank, and NEITHER line is a table
    row. Linear in the length of ``text``.

    A table row is a barrier on BOTH sides, not just below. Blocking only the join
    *into* a row makes the pass order-dependent — the line after a table would fold into
    it, and a second application would then fold the pair upwards, so the transform's
    output depended on how many times it had run
    (``test_normalisation_is_idempotent``). A table interrupts the prose on both sides,
    so it is treated as interrupting on both sides.
    """
    out: list[str] = []
    previous_was_row = False
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            out.append("")
            previous_was_row = False
            continue
        is_row = _is_table_row(line)
        continues = (
            bool(out)
            and bool(out[-1])
            and out[-1][-1] not in _TERMINAL
            and not is_row
            and not previous_was_row
        )
        if continues:
            out[-1] = f"{out[-1]} {line.lstrip()}"
        else:
            out.append(line)
        previous_was_row = is_row
    return "\n".join(out)


def fuse_address_runs(text: str) -> str:
    """Rejoin space-shattered address fragments into the single literal they encode.

    Acts only on a run of two or more single-space-separated, non-word-shaped base58
    fragments that fuse to at least ``_MIN_ADDRESS_LEN`` characters — i.e. only where
    the result could actually satisfy ``sanitize._ADDR``. Every other byte is preserved.
    Linear: each token is visited once.
    """
    parts = _TOKEN.findall(text)
    out: list[str] = []
    index = 0
    total = len(parts)
    while index < total:
        token = parts[index]
        if not _is_address_fragment(token):
            out.append(token)
            index += 1
            continue
        run = [token]
        last = index
        cursor = index + 1
        # Single space only: a wider gap is column alignment, not a split literal.
        while (
            cursor + 1 < total
            and parts[cursor] == " "
            and _is_address_fragment(parts[cursor + 1])
        ):
            run.append(parts[cursor + 1])
            last = cursor + 1
            cursor += 2
        fused = "".join(run)
        if len(run) >= 2 and len(fused) >= _MIN_ADDRESS_LEN:
            out.append(fused)
            index = last + 1
        else:
            out.append(token)
            index += 1
    return "".join(out)


def normalize_recovered_text(text: str) -> str:
    """Undo the renderer's line breaks and shattered address runs, in that order.

    Order matters: an address split across a line boundary is only reachable by the
    fuse once the soft wrap that split it has been undone.

    Pure, deterministic, linear, and total — it never raises, because it runs on
    attacker-supplied pixels on every comprehended image.

    Table rows are excluded from the fuse as well as from the unwrap. Fusing a row's
    cells has no detection value — a row on its own carries no directive — while it is
    the entire false-positive mechanism, so declining is free. It also makes the pass
    IDEMPOTENT: fusing a row would collapse it to one token, erasing the very evidence
    that it was a row, so a second application would unwrap and fuse the whole table into
    one giant pseudo-address (``test_normalisation_is_idempotent`` caught exactly that).
    """
    if not text:
        return text
    lines = unwrap_soft_breaks(text).split("\n")
    return "\n".join(
        line if _is_table_row(line) else fuse_address_runs(line) for line in lines
    )
