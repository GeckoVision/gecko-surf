"""Skill Guard L2 + L3 — deterministic image metadata / trailing-byte / OCR scan.

An image is an untrusted channel: the GhostCommit attack (arXiv:2603.03637)
hides a coding-agent directive where a secret scanner won't look. **L2** extracts
the *text-carrying* channels of an image with **stdlib only** (PNG tEXt/iTXt/zTXt,
JPEG COM/APPn, trailing bytes) — and, when the ``[imagescan]`` Pillow extra is
present, the deep EXIF/XMP/IPTC/ICC metadata. **L3** OCRs the *rendered pixels*
when the ``[ocr]`` extra (pytesseract + the tesseract binary) is present. Every
recovered channel runs through Gecko's EXISTING injection engine
(``sanitize.scan_text`` / ``sanitize.scan_convention_text`` +
``sanitize.looks_like_secret_value``). Any hit quarantines through the same
fail-closed seam as a poisoned spec.

Graceful degradation: both extras are OPTIONAL and lazy-imported. With neither
installed (the base install), :func:`ocr_text` returns ``""`` and
:func:`extract_pillow_metadata` returns ``[]`` — nothing changes, nothing raises.

Honesty ledger (what this module really does):

    We claim (deterministic)                 | We must NEVER claim
    -----------------------------------------|----------------------------------
    Extract PNG tEXt/iTXt/zTXt + JPEG        | "Steganography analysis" / LSB
      COM/APPn text; scan trailing bytes     |   pixel decode
    OCR rendered text → the EXISTING scanner | That we *decode* hidden pixel
      (L3)                                   |   payloads
    Name the channel + rule that fired       | Any ML confidence % ("99.2%")
    Reuse the sanitize engine unchanged      | "First tool to detect this"

Named residual: without the OCR extra, L2 CANNOT see a payload rendered as
*visible pixels* — the flagship GhostCommit attack carries NO metadata and NO
trailing bytes (``build_spec_payload.png`` proves it). But even with OCR absent,
the L1 convention-text scan (``sanitize.scan_convention_text``) still catches the
delivery file. base64/numeric-encoded payloads *inside* OCR'd text remain a named
residual; the auth-host pin + recorded-mode scrub are the real containment. This
module is a strong deterministic link, not the whole chain.

Control plane (invariant #1): the verdict carries channel names, rule names, and
byte-counts — never the extracted payload text and never a decoded secret value.

Regression guard: extracted image text is scanned for INJECTION + SECRET only.
``looks_like_address_value`` is NEVER called here — a bare wallet address in
metadata is DATA, not a routing directive, and must not quarantine (protects the
base58 false-positive fix). ``wallet_addr_exif.png`` verdicts ``clean``.
"""

from __future__ import annotations

import io
import struct
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

from gecko import encdetect, sanitize

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
# A crafted image the decoder REFUSES (huge declared dimensions in a tiny file →
# Pillow DecompressionBombError, or otherwise undecodable image-signatured bytes).
# Bytes that break the scanner are the attack signal, so this is a fail-closed
# anomaly (→ review), never silently "clean". Named, never the payload.
_ANOMALY_SCAN_ERROR = "scan-error"

_JPEG_SIGNATURE = b"\xff\xd8"


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


# --- L3: OCR the rendered pixels (opt-in [ocr] extra) --------------------------------


def ocr_text(data: bytes) -> str:
    """Recover the text rendered as *visible pixels* in an image (L3).

    Graceful-degradation contract: if ``pytesseract``/``PIL`` is not installed OR the
    tesseract binary is absent (or the bytes are not a decodable image), return ``""``
    — this function NEVER raises. So the base install, and any environment without the
    ``[ocr]`` extra, are completely unaffected. Only when the extra is present does this
    read the pixels; the recovered text is then scanned by the SAME injection engine.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        with Image.open(io.BytesIO(data)) as img:
            recovered = pytesseract.image_to_string(img)
    except Exception:
        # This function's contract is "never raises, return '' on failure". A broad
        # catch is correct for an untrusted-input parser: OSError covers
        # TesseractNotFoundError and PIL's UnidentifiedImageError / truncated-image
        # OSError, ValueError covers bad modes, TesseractError a runtime failure — but
        # Pillow also raises DecompressionBombError (a bare Exception subclass) on a
        # crafted huge-dimension image, and that MUST NOT crash comprehension. OCR is
        # best-effort; any failure degrades to "" (fail-closed is enforced upstream).
        return ""
    return recovered or ""


# --- L2 deep metadata via Pillow (opt-in [imagescan] extra) --------------------------


def _collect_exif_ifd(ifd: Any, prefix: str, channels: list[TextChannel]) -> None:
    """Append each text-valued EXIF tag in one IFD as a channel (tag name label)."""
    from PIL import ExifTags

    for tag_id, value in ifd.items():
        if isinstance(value, bytes):
            value = value.decode("latin-1", "replace")
        if isinstance(value, str) and value:
            name = ExifTags.TAGS.get(tag_id, hex(tag_id))
            channels.append(TextChannel(f"exif:{name}", value))


def _looks_like_image(data: bytes) -> bool:
    """True when ``data`` carries a PNG/JPEG signature — i.e. it *claims* to be an
    image. Only signatured-but-undecodable bytes count as a scan-error anomaly; random
    non-image bytes are simply not an image (no anomaly)."""
    return data.startswith(_PNG_SIGNATURE) or data.startswith(_JPEG_SIGNATURE)


def _pillow_metadata(data: bytes) -> tuple[list[TextChannel], str | None]:
    """Deep Pillow metadata channels + an optional scan-error anomaly, in ONE decode.

    Returns ``(channels, None)`` normally. Returns ``(channels, _ANOMALY_SCAN_ERROR)``
    when Pillow is present but REFUSES to open image-signatured bytes — a crafted
    decompression bomb (huge declared dimensions in a tiny file → Pillow raises
    ``DecompressionBombError``, a bare :class:`Exception` subclass) or otherwise
    undecodable PNG/JPEG. That failure on attacker bytes is the attack signal, so it is
    surfaced as a fail-closed anomaly (→ ``review``) rather than being swallowed into a
    false ``clean``. Pillow absent → ``([], None)`` (base install unaffected; it cannot
    decode images at all, so it neither sees nor crashes on the bomb). Never raises.
    """
    try:
        from PIL import ExifTags, Image
    except ImportError:
        return [], None
    channels: list[TextChannel] = []
    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
            _collect_exif_ifd(exif, "exif", channels)
            try:
                sub = exif.get_ifd(ExifTags.IFD.Exif)
            except (KeyError, ValueError):
                sub = {}
            _collect_exif_ifd(sub, "exif", channels)
            # info carries XMP, comments, IPTC as strings. Only str values are scanned:
            # raw ICC/EXIF byte blobs are binary noise (EXIF is already read above), so
            # decoding them adds nothing but false channels.
            for key, value in getattr(img, "info", {}).items():
                if isinstance(value, str) and value:
                    channels.append(TextChannel(f"pillow:info:{key}", value))
    except Exception:
        # Broad by contract: this never raises. OSError/ValueError cover a non-decodable
        # or truncated image, but Pillow also raises DecompressionBombError (a bare
        # Exception subclass) on a crafted huge-dimension image — catching only
        # (OSError, ValueError) let it propagate and crash comprehension. Image-signatured
        # bytes the decoder refuses are an anomaly (fail-closed); genuinely non-image bytes
        # are not (they just aren't an image). Degrade to whatever channels were collected.
        return channels, (_ANOMALY_SCAN_ERROR if _looks_like_image(data) else None)
    return channels, None


def extract_pillow_metadata(data: bytes) -> list[TextChannel]:
    """Deep image metadata via Pillow: EXIF (base + Exif IFD), XMP / ``info`` strings,
    IPTC, ICC. Returns ``[]`` when Pillow is absent (base install unaffected) and never
    raises. Kept OUT of :func:`extract_text_channels` (which stays stdlib-only) — merged
    into :func:`scan_image`'s channel set — so a plain install's L2 behaviour is identical
    with or without the ``[imagescan]`` extra. Thin wrapper over :func:`_pillow_metadata`
    that drops the scan-error signal (which :func:`scan_image` consumes as an anomaly)."""
    channels, _scan_error = _pillow_metadata(data)
    return channels


# --- verdict -------------------------------------------------------------------------


def _ocr_hits(text: str) -> list[str]:
    """Injection basis for OCR-recovered rendered text, on the ``ocr`` channel.

    The rendered pixels ARE an untrusted instruction doc (the GhostCommit derivation
    rule lives in the image, not the delivery file), so they run through
    ``sanitize.scan_convention_text`` — the SAME engine as an ingested convention file,
    which catches the follow-rendered + numeric-encode-exfil COMBINATION that plain
    ``scan_text`` misses. NEVER ``looks_like_address_value`` (the base58 FP guard extends
    to OCR text). Capped at ``_MAX_SCAN_TEXT`` like every other channel — OCR of a large
    image can return a lot of text, and the fold-heavy scanner would OOM on it otherwise.
    """
    text = text[:_MAX_SCAN_TEXT]
    basis = [f"ocr → {rule}" for rule in sanitize.scan_convention_text(text)]
    if sanitize.looks_like_secret_value(text):
        basis.append("ocr → secret_value")
    return basis


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


def _decode_hits(channel: str, text: str) -> list[str]:
    """Encoding-aware basis strings for one channel's text (Skill Guard enhancement a).

    Decode base64/hex/rot13 blobs in the channel text and rescan the DECODED plaintext
    with the sanitize engine (``encdetect.decode_and_rescan``). A hit is **poison**: we
    have DECODED and found real malice (unlike the LSB presence-detector). The basis names
    ``channel(encoding) → rule`` (e.g. ``png:tEXt(base64) → exfil_encoded_target``) —
    encoding + channel + rule NAMES only, NEVER the decoded payload (control plane). The
    decode path NEVER calls ``looks_like_address_value`` (base58 FP guard), and a benign
    encoded blob decodes to a benign string and contributes nothing (semantic, not
    presence)."""
    return [
        f"{channel}({encoding}) → {rule}"
        for encoding, rule in encdetect.decode_and_rescan(text)
    ]


def scan_image(
    data: bytes, *, ocr: Callable[[bytes], str] | None = None
) -> ImageScanVerdict:
    """Deterministic verdict for one image (L2 + L3).

    Every extracted channel — stdlib metadata (:func:`extract_text_channels`), the
    trailing-byte payload, Pillow deep metadata (:func:`extract_pillow_metadata`, when
    the ``[imagescan]`` extra is present), and the OCR'd rendered pixels (``ocr``, when
    the ``[ocr]`` extra is present) — is run through the sanitize engine. ANY hit →
    ``poison`` (basis names the channel + rule). A structural anomaly with no hit →
    ``review``. Otherwise ``clean``.

    The OCR seam is INJECTABLE: pass ``ocr=`` a callable for offline, tesseract-free
    tests of the OCR-text → verdict wiring; it defaults to the real :func:`ocr_text`
    (which returns ``""`` — a no-op — when the extra/binary is absent).

    ``looks_like_address_value`` is deliberately never invoked: a bare wallet address in
    metadata or OCR text is data, not a routing directive (base58 FP guard).
    """
    ocr_fn = ocr if ocr is not None else ocr_text
    channels = list(extract_text_channels(data))
    pillow_channels, scan_error = _pillow_metadata(data)
    channels.extend(pillow_channels)
    trailer = find_trailing_bytes(data)
    anomalies = structural_anomalies(data)
    # A decoder that REFUSES attacker bytes (e.g. a decompression bomb) is itself an
    # anomaly — surface it as review, never let a scan failure pass as clean.
    if scan_error is not None:
        anomalies.append(scan_error)

    scanned = len(channels)
    hits: list[str] = []
    for ch in channels:
        hits.extend(_channel_hits(ch.channel, ch.text))
        # Enhancement (a): decode base64/hex/rot13 blobs in the channel and rescan the
        # plaintext — catches an injection/exfil directive hidden under an encoding.
        hits.extend(_decode_hits(ch.channel, ch.text))

    if trailer is not None:
        scanned += 1
        # Label carries the TRUE trailer size (anomaly signal preserved); only the
        # bytes we DECODE + SCAN are capped, so a 2 GB trailer never becomes a str.
        label = f"png:trailing-bytes({_fmt_size(len(trailer))})"
        scan_text = trailer[:_MAX_SCAN_TEXT].decode("latin-1", "replace")
        hits.extend(_channel_hits(label, scan_text))
        hits.extend(_decode_hits(label, scan_text))

    # L3: OCR the rendered pixels. In the base install / this env (no tesseract) the
    # default ocr_fn returns "" here, so this is a no-op and the L2 verdict stands.
    recovered = ocr_fn(data)
    if recovered:
        scanned += 1
        hits.extend(_ocr_hits(recovered))
        # The rendered pixels are the GhostCommit delivery channel; an encoded payload
        # there (base64/hex/rot13) is decoded + rescanned like any other channel.
        hits.extend(_decode_hits("ocr", recovered))

    if hits:
        return ImageScanVerdict("poison", tuple(hits), scanned)
    if anomalies:
        basis = tuple(f"{a} → structural-anomaly" for a in anomalies)
        return ImageScanVerdict("review", basis, scanned)
    return ImageScanVerdict("clean", (), scanned)
