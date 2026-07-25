"""Encoding-aware decode + rescan for Skill Guard — closes the base64/hex/rot13 residual.

An attacker can base64/hex/rot13-encode an injection or exfil directive inside image
metadata or OCR'd text so the raw string never trips the curated pattern engine
(``sanitize``). GhostCommit (arXiv:2603.03637) does exactly this: it exfiltrates a key
*encoded as numbers* to dodge string-shaped scanners. This module finds encoded blobs,
decodes them under a strict printable-UTF-8 gate, and re-runs the EXISTING ``sanitize``
engine on the DECODED plaintext. It flags ONLY when the decoded content is actually
malicious — a benign base64 marker decodes to a benign string and stays CLEAN. This is a
SEMANTIC check (decode → real malice), never a presence detector.

Because we have DECODED and found real malice, a hit contributes **poison** (unlike the
LSB presence-detector, which can only justify review).

Guard rails (each is a deliberate red-team answer):

  * printable-UTF-8 gate (``_MIN_PRINTABLE_RATIO``) — the PRIMARY false-positive guard.
    Random base58 addresses, sha256 hashes, and JWT signature segments decode to binary
    garbage → rejected before any rescan. A legitimate address/hash in metadata never
    quarantines.
  * semantic rescan — ``sanitize.scan_convention_text`` (which subsumes ``scan_text`` and
    adds the numeric-exfil signature) runs on the decoded plaintext. Presence of an
    encoding is not malice; only a malicious DECODE fires.
  * base58 regression guard — ``looks_like_address_value`` is NEVER called here. A bare
    decoded wallet address is DATA, not a routing directive.
  * bounded work — the swept text is capped at ``_MAX_SCAN_TEXT`` (signatures are short,
    so truncation opens no bypass) and decoded output is bounded by the capped blob, so a
    multi-megabyte blob cannot OOM the fold-heavy scanner.

Control plane (invariant #1): this module returns ``(encoding, rule)`` pairs — an encoding
NAME and a rule NAME — NEVER the decoded payload. Callers name encoding + channel + rule.

NAMED RESIDUAL (do NOT try to close by brute force): XOR / multi-byte-key and other
keyed encodings are not swept — the key space is too large for a blind decode, and
brute-forcing keys would be both a DoS and a false-positive factory. That residual is
contained downstream (the auth-host pin in ``caller.py``, the recorded-mode response
scrub, and quarantine-on-detect), never claimed as caught here.
"""

from __future__ import annotations

import base64
import binascii
import re

from gecko import sanitize

# Same scan cap as imagescan's channels (both derive from sanitize.MAX_TEXT_LEN). Defined
# independently to avoid importing imagescan (which imports THIS module). Injection/exfil
# signatures are short — well under sanitize's per-field cap — so truncating the text we
# SWEEP opens no bypass while bounding the fold-heavy rescan.
_MAX_SCAN_TEXT = 16 * sanitize.MAX_TEXT_LEN  # ~9.4 KiB

# Minimum share of a decoded blob that must be printable UTF-8 for it to be rescanned.
# Below this the blob is binary garbage (a base58 address, a hash, a signature segment) —
# reject it rather than rescan. This is the primary false-positive guard.
_MIN_PRINTABLE_RATIO = 0.9

# base64: standard charset, ≥ 16 core chars (short runs are noise), optional 1–2 padding.
# The len % 4 == 0 requirement is enforced after the match (see _decode_base64).
_BASE64_RUN: re.Pattern[str] = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
# hex: ≥ 32 chars. Even length is enforced after the match (see _decode_hex).
_HEX_RUN: re.Pattern[str] = re.compile(r"[0-9a-fA-F]{32,}")

# rot13 is a cheap, keyless, self-inverse transform, so we apply it to the whole text and
# rescan — no blob detection needed. A benign string rot13s to gibberish (clean); only a
# rot13-ENCODED injection re-forms into a real trigger. Manual table (not the codec) keeps
# it str-typed and dependency-free. Digits/punctuation pass through unchanged.
_ROT13 = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm",
)


def _printable_utf8(raw: bytes) -> str | None:
    """Decode ``raw`` as UTF-8 and return it only if it is *mostly printable*
    (``_MIN_PRINTABLE_RATIO``). ``None`` == binary garbage → do not rescan.

    This is what keeps a base58 address / sha256 hash / JWT signature (which decode to
    non-text bytes) from ever reaching the rescan: they are rejected here, so they never
    quarantine. Tab/newline/CR count as printable (they are legitimate in text payloads);
    ``str.isprintable`` already treats a space as printable.
    """
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\t\n\r")
    if printable / len(text) < _MIN_PRINTABLE_RATIO:
        return None
    return text


def _decode_base64(blob: str) -> str | None:
    """Strictly decode a candidate base64 blob to printable UTF-8, else ``None``."""
    if len(blob) % 4 != 0:
        return None
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return None
    return _printable_utf8(raw)


def _decode_hex(blob: str) -> str | None:
    """Strictly decode a candidate hex blob to printable UTF-8, else ``None``."""
    if len(blob) % 2 != 0:
        return None
    try:
        raw = bytes.fromhex(blob)
    except ValueError:
        return None
    return _printable_utf8(raw)


def _rescan(plaintext: str) -> list[str]:
    """Rule names the decoded plaintext trips. ``scan_convention_text`` subsumes
    ``scan_text`` (same engine) and adds the numeric-exfil signature, so one call covers
    both a blunt injection and a GhostCommit-style encoded-exfil directive. NEVER calls
    ``looks_like_address_value`` — the base58 FP guard extends to decoded text."""
    return sanitize.scan_convention_text(plaintext[:_MAX_SCAN_TEXT])


def decode_and_rescan(text: str) -> list[tuple[str, str]]:
    """Find encoded blobs in ``text``, decode them under the printable-UTF-8 gate, and
    rescan the plaintext with the sanitize engine.

    Returns a deduplicated list of ``(encoding, rule)`` pairs — encoding is one of
    ``"base64"`` / ``"hex"`` / ``"rot13"``, rule is a ``sanitize`` rule name. Empty list ==
    nothing decoded to something malicious (CLEAN). NEVER returns the decoded payload
    (control plane): the caller names encoding + channel + rule only.
    """
    if not text:
        return []
    text = text[:_MAX_SCAN_TEXT]
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(encoding: str, plaintext: str | None) -> None:
        if plaintext is None:
            return
        for rule in _rescan(plaintext):
            key = (encoding, rule)
            if key not in seen:
                seen.add(key)
                found.append(key)

    for match in _BASE64_RUN.finditer(text):
        _add("base64", _decode_base64(match.group()))
    for match in _HEX_RUN.finditer(text):
        _add("hex", _decode_hex(match.group()))
    # rot13: whole-text transform, always rescanned. Benign text → gibberish → clean.
    _add("rot13", text.translate(_ROT13))

    return found
