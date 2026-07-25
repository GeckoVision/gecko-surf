"""L2 image metadata / trailing-byte scan (Skill Guard PR2).

The four generated PNGs are committed; ``make_fixtures`` is imported for the
helpers that craft the decompression-bomb case without a committed fixture.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


from gecko import imagescan, sanitize

FIXTURES = Path(__file__).parent / "fixtures" / "imagescan"


def _load_make_fixtures():
    """Import the stdlib fixture generator by path (it lives under fixtures/)."""
    path = FIXTURES / "make_fixtures.py"
    spec = importlib.util.spec_from_file_location("imagescan_make_fixtures", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


make_fixtures = _load_make_fixtures()


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- scan_image verdicts -------------------------------------------------------------


def test_poison_exif_is_poison_naming_text_chunk_and_rule():
    verdict = imagescan.scan_image(_read("poison_exif.png"))
    assert verdict.tier == "poison"
    assert any("png:tEXt" in b and "prompt_injection" in b for b in verdict.basis)
    assert verdict.channels_scanned >= 1


def test_poison_trailer_is_poison_naming_trailing_bytes():
    verdict = imagescan.scan_image(_read("poison_trailer.png"))
    assert verdict.tier == "poison"
    assert any("trailing-bytes" in b for b in verdict.basis)


def test_clean_arch_is_clean():
    verdict = imagescan.scan_image(_read("clean_arch.png"))
    assert verdict.tier == "clean"
    assert verdict.basis == ()


def test_wallet_addr_is_clean_base58_regression_guard():
    """THE key assert: a bare base58 address in metadata is DATA, not a routing
    directive. It must verdict clean — never quarantine (base58 FP fix)."""
    verdict = imagescan.scan_image(_read("wallet_addr_exif.png"))
    assert verdict.tier == "clean"
    # The address channel WAS scanned — it just didn't (and must not) hit.
    assert verdict.channels_scanned >= 1


def test_looks_like_address_value_never_called_on_image_text(monkeypatch):
    """Hard constraint: the address detector is never invoked on image text."""

    def _boom(_value):  # pragma: no cover - fails the test if ever reached
        raise AssertionError("looks_like_address_value called on image text")

    monkeypatch.setattr(sanitize, "looks_like_address_value", _boom)
    for name in ("wallet_addr_exif.png", "poison_exif.png", "clean_arch.png"):
        imagescan.scan_image(_read(name))


def test_build_spec_payload_is_clean_at_l2():
    """The flagship GhostCommit image: payload is rendered as PIXELS, with no
    metadata and no trailing bytes. L2 alone misses it — this documents why
    PR3/OCR exists. Verdict is clean at L2."""
    data = _read("build_spec_payload.png")
    assert imagescan.find_trailing_bytes(data) is None
    channels = imagescan.extract_text_channels(data)
    for ch in channels:
        assert not sanitize.scan_text(ch.text)
    assert imagescan.scan_image(data).tier == "clean"


# --- extractor unit tests ------------------------------------------------------------


def test_extract_text_channels_recovers_text_chunk():
    channels = imagescan.extract_text_channels(_read("poison_exif.png"))
    assert any(
        ch.channel == "png:tEXt" and "ignore previous" in ch.text for ch in channels
    )


def test_extract_text_channels_ztxt_roundtrip():
    png = make_fixtures.assemble_png(
        extra_chunks=(make_fixtures.ztxt_chunk("Comment", "hello compressed world"),)
    )
    channels = imagescan.extract_text_channels(png)
    assert any(
        ch.channel == "png:zTXt" and "hello compressed" in ch.text for ch in channels
    )


def test_find_trailing_bytes_recovers_appended_payload():
    trailer = imagescan.find_trailing_bytes(_read("poison_trailer.png"))
    assert trailer is not None
    assert b"ignore previous instructions" in trailer


def test_find_trailing_bytes_none_for_clean_png():
    assert imagescan.find_trailing_bytes(_read("clean_arch.png")) is None


def test_structural_anomalies_names_trailing_payload():
    anomalies = imagescan.structural_anomalies(_read("poison_trailer.png"))
    assert any("trailing-bytes" in a for a in anomalies)


def test_structural_anomalies_empty_for_clean_png():
    assert imagescan.structural_anomalies(_read("clean_arch.png")) == []


def test_not_an_image_returns_empty_never_raises():
    assert imagescan.extract_text_channels(b"not an image at all") == []
    assert imagescan.find_trailing_bytes(b"not an image") is None
    assert imagescan.structural_anomalies(b"") == []


def test_truncated_png_does_not_raise():
    data = _read("poison_exif.png")[:20]
    assert imagescan.extract_text_channels(data) == [] or True  # must not raise
    imagescan.scan_image(data)  # must not raise


# --- decompression-bomb cap ----------------------------------------------------------


def test_ztxt_decompression_bomb_is_anomaly_not_oom():
    """A crafted zTXt that inflates far past the cap must be reported as a
    structural anomaly — never inflated into an OOM, never yielded as a channel."""
    bomb_text = "A" * (imagescan._INFLATE_CAP * 8)  # ~8 MiB, compresses tiny
    png = make_fixtures.assemble_png(
        extra_chunks=(make_fixtures.ztxt_chunk("Comment", bomb_text),)
    )
    # Not inflated into a channel.
    assert imagescan.extract_text_channels(png) == []
    # Surfaced as an anomaly instead.
    assert imagescan._ANOMALY_OVERSIZED in imagescan.structural_anomalies(png)
    # And it drives a review verdict (structural anomaly, no injection hit).
    assert imagescan.scan_image(png).tier == "review"


def test_review_when_benign_trailer_no_injection():
    png = make_fixtures.assemble_png(trailer=b"just some benign appended bytes")
    verdict = imagescan.scan_image(png)
    assert verdict.tier == "review"
    assert any("trailing-bytes" in b for b in verdict.basis)


def test_verdict_basis_carries_no_payload_text():
    """Control-plane: the verdict names channels + rules, never the payload."""
    verdict = imagescan.scan_image(_read("poison_exif.png"))
    for b in verdict.basis:
        assert "ignore previous instructions" not in b
        assert ".env" not in b
