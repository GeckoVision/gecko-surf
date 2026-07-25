"""Skill Guard L2 — deterministic image metadata / trailing-byte scan.

An image is an untrusted channel: the GhostCommit attack (arXiv:2603.03637)
hides a coding-agent directive where a secret scanner won't look. L2 extracts
the *text-carrying* channels of an image with **stdlib only** — no Pillow, no
OCR (those are L3/PR3) — and runs them through Gecko's EXISTING injection engine
(``sanitize.scan_text`` + ``sanitize.looks_like_secret_value``). Any hit
quarantines through the same fail-closed seam as a poisoned spec.

Honesty ledger (what this module really does):

    We claim (deterministic)                 | We must NEVER claim
    -----------------------------------------|----------------------------------
    Extract PNG tEXt/iTXt/zTXt + JPEG        | "Steganography analysis" / LSB
      COM/APPn text; scan trailing bytes     |   pixel decode
    Name the channel + rule that fired       | Any ML confidence % ("99.2%")
    Reuse the sanitize engine unchanged      | "First tool to detect this"

Named residual: L2 CANNOT see a payload rendered as *visible pixels* — that is
the flagship GhostCommit attack, and it carries NO metadata and NO trailing
bytes (``build_spec_payload.png`` proves it). Reading rendered pixels is L3/OCR.
With no OCR extra installed, the L1 convention-text scan still catches the
delivery file. This module is a strong deterministic link, not the whole chain.

Control plane (invariant #1): the verdict carries channel names, rule names, and
byte-counts — never the extracted payload text and never a decoded secret value.

Regression guard: extracted image text is scanned for INJECTION + SECRET only.
``looks_like_address_value`` is NEVER called here — a bare wallet address in
metadata is DATA, not a routing directive, and must not quarantine (protects the
base58 false-positive fix). ``wallet_addr_exif.png`` verdicts ``clean``.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from gecko import sanitize

# Verdict tiers. No canonical image-tier Literal exists elsewhere (risk.py's
# ``Tier`` is governance-scoped), so this module is the single source of truth.
ImageTier = Literal["clean", "review", "poison"]

# Output cap for inflating a compressed metadata chunk (zTXt / compressed iTXt).
# A chunk that inflates past this is a decompression bomb: we refuse to inflate
# it and record a structural anomaly instead of risking an OOM.
_INFLATE_CAP = 1 << 20  # 1 MiB

# Cap on the text actually handed to the injection/secret scanner. sanitize.scan_text
# folds the WHOLE string (NFKC normalize + confusable translate = several transient
# copies) before it matches, so an *uncompressed* multi-gigabyte channel — a trailing
# payload or a huge tEXt/iTXt body, none bounded by _INFLATE_CAP — would OOM. Injection
# and secret signatures are short (well under sanitize's per-field MAX_TEXT_LEN), so
# truncating the SCANNED text opens no bypass. The structural-anomaly path still measures
# the FULL byte-count (see structural_anomalies / scan_image), so a huge trailer is still
# surfaced with its true size — this cap shrinks what we SCAN, never what we REPORT.
_MAX_SCAN_TEXT = 16 * sanitize.MAX_TEXT_LEN  # ~9.4 KiB — orders above any signature

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Anomaly names (control-plane safe — names only, never payload).
_ANOMALY_OVERSIZED = "png:oversized-metadata"


@dataclass(frozen=True)
class TextChannel:
    """A text-carrying channel recovered from an image (channel label, text)."""

    channel: str
    text: str


@dataclass(frozen=True)
class ImageScanVerdict:
    """The deterministic verdict for one image.

    ``basis`` names WHY (channel + rule, or a structural anomaly) — never the
    payload. ``channels_scanned`` counts the text channels (metadata + trailing
    bytes) actually run through the engine.
    """

    tier: ImageTier
    basis: tuple[str, ...]
    channels_scanned: int


# --- PNG parsing ---------------------------------------------------------------------


def _iter_png_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield ``(chunk_type, chunk_data)`` for each PNG chunk.

    Truncation-safe: stops cleanly the moment there are not enough bytes for the
    next chunk header/body, never raising on a malformed or partial file.
    """
    pos = len(_PNG_SIGNATURE)
    end = len(data)
    while pos + 8 <= end:
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        body_start = pos + 8
        body_end = body_start + length
        if body_end > end:
            return  # truncated chunk — stop, don't raise
        yield ctype, data[body_start:body_end]
        pos = body_end + 4  # skip the 4-byte CRC


def _inflate_capped(comp: bytes) -> str | None:
    """Inflate ``comp`` but refuse past ``_INFLATE_CAP``. ``None`` == bomb or
    corrupt (the caller records a structural anomaly rather than inflating)."""
    try:
        dobj = zlib.decompressobj()
        out = dobj.decompress(comp, _INFLATE_CAP + 1)
        if len(out) > _INFLATE_CAP or dobj.unconsumed_tail:
            return None  # would exceed the cap: treat as a decompression bomb
        out += dobj.flush()
    except zlib.error:
        return None
    if len(out) > _INFLATE_CAP:
        return None
    return out.decode("latin-1", "replace")


def _png_text(data: bytes) -> tuple[list[TextChannel], list[str]]:
    """Extract PNG text channels and note structural anomalies (oversized
    metadata). One parse feeds both the extractor and the anomaly reporter."""
    channels: list[TextChannel] = []
    anomalies: list[str] = []
    for ctype, body in _iter_png_chunks(data):
        if ctype == b"tEXt":
            _, _, text = body.partition(b"\x00")
            # tEXt is uncompressed and bounded only by file size — cap the BYTES
            # before decoding so a gigabyte body never materializes as a str.
            channels.append(
                TextChannel(
                    "png:tEXt", text[:_MAX_SCAN_TEXT].decode("latin-1", "replace")
                )
            )
        elif ctype == b"zTXt":
            _keyword, _, rest = body.partition(b"\x00")
            comp = rest[1:]  # skip the 1-byte compression method
            ztext = _inflate_capped(comp)
            if ztext is None:
                anomalies.append(_ANOMALY_OVERSIZED)
            else:
                channels.append(TextChannel("png:zTXt", ztext))
        elif ctype == b"iTXt":
            channel_text = _itxt_text(body)
            if channel_text is None:
                anomalies.append(_ANOMALY_OVERSIZED)
            else:
                channels.append(TextChannel("png:iTXt", channel_text))
    return channels, anomalies


def _itxt_text(body: bytes) -> str | None:
    """Decode an ``iTXt`` chunk body; ``None`` if a compressed one is a bomb.

    Layout: keyword \\0 compression_flag(1) compression_method(1)
    language_tag \\0 translated_keyword \\0 text.
    """
    _keyword, _, rest = body.partition(b"\x00")
    if len(rest) < 2:
        return ""
    compression_flag = rest[0]
    rest = rest[2:]  # drop flag + method
    _lang, _, rest = rest.partition(b"\x00")
    _translated, _, text_bytes = rest.partition(b"\x00")
    if compression_flag == 1:
        return _inflate_capped(text_bytes)
    # Uncompressed iTXt is bounded only by file size — cap bytes before decoding.
    return text_bytes[:_MAX_SCAN_TEXT].decode("utf-8", "replace")


# --- JPEG parsing --------------------------------------------------------------------


def _jpeg_text(data: bytes) -> list[TextChannel]:
    """Extract JPEG ``COM`` and ``APPn`` marker payloads as text channels."""
    channels: list[TextChannel] = []
    pos = 2  # skip SOI (FF D8)
    end = len(data)
    while pos + 4 <= end and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA:  # SOS — entropy-coded scan follows; stop marker walk
            break
        (seg_len,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        seg_start = pos + 4
        seg_end = pos + 2 + seg_len
        if seg_end > end:
            break
        payload = data[seg_start:seg_end]
        if marker == 0xFE:  # COM
            channels.append(
                TextChannel("jpeg:COM", payload.decode("latin-1", "replace"))
            )
        elif 0xE0 <= marker <= 0xEF:  # APPn
            channels.append(
                TextChannel(
                    f"jpeg:APP{marker - 0xE0}", payload.decode("latin-1", "replace")
                )
            )
        pos = seg_end
    return channels


# --- public extraction API -----------------------------------------------------------


def extract_text_channels(data: bytes) -> list[TextChannel]:
    """Every text-carrying channel of a PNG or JPEG. Not-an-image / truncated
    bytes → ``[]`` (never raises). Compressed chunks are inflated under a cap;
    a decompression bomb is dropped here and surfaced by :func:`structural_anomalies`.
    """
    if data.startswith(_PNG_SIGNATURE):
        channels, _ = _png_text(data)
        return channels
    if data.startswith(b"\xff\xd8"):
        return _jpeg_text(data)
    return []


def find_trailing_bytes(data: bytes) -> bytes | None:
    """Bytes appended after the PNG ``IEND`` chunk / JPEG ``FFD9`` EOI, or
    ``None`` when there are none. This is how the ``poison_trailer.png`` payload
    is recovered; ``clean_arch.png`` and ``build_spec_payload.png`` return
    ``None`` (the latter proves L2 alone misses the rendered-pixel attack).
    """
    if data.startswith(_PNG_SIGNATURE):
        marker = data.rfind(b"IEND")
        if marker == -1:
            return None
        tail = data[marker + 8 :]  # IEND data is empty; +4 type +4 CRC
        return tail or None
    if data.startswith(b"\xff\xd8"):
        marker = data.rfind(b"\xff\xd9")
        if marker == -1:
            return None
        tail = data[marker + 2 :]
        return tail or None
    return None


def structural_anomalies(data: bytes) -> list[str]:
    """Named structural anomalies — a trailing payload, oversized/uninflatable
    metadata. NAMES only, no decoding. Not-an-image / truncated → ``[]``.
    """
    anomalies: list[str] = []
    if data.startswith(_PNG_SIGNATURE):
        _, oversized = _png_text(data)
        anomalies.extend(oversized)
    trailer = find_trailing_bytes(data)
    if trailer is not None:
        anomalies.append(f"png:trailing-bytes({_fmt_size(len(trailer))})")
    return anomalies


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    return f"{n / 1024:.1f}KB"


# --- verdict -------------------------------------------------------------------------


def _channel_hits(channel: str, text: str) -> list[str]:
    """Injection/secret basis strings for one channel's text. Reuses the
    sanitize engine unchanged; NEVER calls ``looks_like_address_value``.

    Belt-and-suspenders cap: even if a caller hands oversized text, we truncate
    before the fold-heavy scanner. Head-of-buffer truncation preserves detection
    because signatures are short (see ``_MAX_SCAN_TEXT``)."""
    text = text[:_MAX_SCAN_TEXT]
    basis = [f"{channel} → {rule}" for rule in sanitize.scan_text(text)]
    if sanitize.looks_like_secret_value(text):
        basis.append(f"{channel} → secret_value")
    return basis


def scan_image(data: bytes) -> ImageScanVerdict:
    """Deterministic verdict for one image (L2).

    Every extracted metadata channel and the trailing-byte payload is run through
    ``sanitize.scan_text`` + ``sanitize.looks_like_secret_value``. ANY hit →
    ``poison`` (basis names the channel + rule). A structural anomaly with no hit
    → ``review``. Otherwise ``clean``.

    ``looks_like_address_value`` is deliberately never invoked: a bare wallet
    address in metadata is data, not a routing directive (base58 FP guard).
    """
    channels = extract_text_channels(data)
    trailer = find_trailing_bytes(data)
    anomalies = structural_anomalies(data)

    scanned = len(channels)
    hits: list[str] = []
    for ch in channels:
        hits.extend(_channel_hits(ch.channel, ch.text))

    if trailer is not None:
        scanned += 1
        # Label carries the TRUE trailer size (anomaly signal preserved); only the
        # bytes we DECODE + SCAN are capped, so a 2 GB trailer never becomes a str.
        label = f"png:trailing-bytes({_fmt_size(len(trailer))})"
        scan_text = trailer[:_MAX_SCAN_TEXT].decode("latin-1", "replace")
        hits.extend(_channel_hits(label, scan_text))

    if hits:
        return ImageScanVerdict("poison", tuple(hits), scanned)
    if anomalies:
        basis = tuple(f"{a} → structural-anomaly" for a in anomalies)
        return ImageScanVerdict("review", basis, scanned)
    return ImageScanVerdict("clean", (), scanned)
