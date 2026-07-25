"""Tests for the `gecko scan-image` / `gecko scan-doc` demo surface (gecko.cli).

Offline, $0. These are the runnable primitives Video 1 shows — a legible
POISON/REVIEW/CLEAN verdict + basis, with a non-zero exit on poison. The CLI is
thin transport over ``imagescan.scan_image`` / ``docs_reader.scan.scan_doc_page``;
these tests exercise the entrypoint (``cli.main``) and assert the exit-code
convention, the header, the basis lines, and the control-plane guarantee (no
payload text ever reaches stdout — only channel/rule names).
"""

from __future__ import annotations

from pathlib import Path

from gecko import cli, imagescan

_FIX = Path(__file__).resolve().parent / "fixtures" / "imagescan"

# The injected exfil payload string that must NEVER appear in CLI output (control
# plane: the basis is name-only). Present verbatim in the delivery/metadata fixtures.
_PAYLOAD_MARKERS = (".env", "_PROV_CANARY", "byte-for-byte")


def _assert_no_payload(out: str) -> None:
    for marker in _PAYLOAD_MARKERS:
        assert marker not in out, f"payload marker {marker!r} leaked to stdout"


# --- gecko scan-doc ----------------------------------------------------------


def test_scan_doc_poison_nonzero_exit_and_basis(capsys) -> None:
    rc = cli.main(["scan-doc", str(_FIX / "agents_delivery.md")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "POISON" in out
    # basis names both L1 signals (rule names, not the payload text)
    assert "follow_rendered_instructions" in out
    assert "exfil_encoded_target" in out
    _assert_no_payload(out)


def test_scan_doc_clean_convention_exit_zero(capsys) -> None:
    # A benign convention file (only the follow-signal, no exfil target) must not
    # quarantine — the L1 FP discipline, surfaced at the CLI.
    rc = cli.main(["scan-doc", str(_FIX / "clean_convention.md")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLEAN" in out


# --- gecko scan-image --------------------------------------------------------


def test_scan_image_poison_exif_nonzero_exit(capsys) -> None:
    # L2 tEXt-chunk injection — caught with no OCR extra, on the base install.
    rc = cli.main(["scan-image", str(_FIX / "poison_exif.png")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "POISON" in out
    assert "prompt_injection" in out
    # channel names appear; the payload does not
    assert "png:tEXt" in out
    _assert_no_payload(out)


def test_scan_image_clean_arch_exit_zero(capsys) -> None:
    rc = cli.main(["scan-image", str(_FIX / "clean_arch.png")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLEAN" in out


def test_scan_image_build_spec_clean_at_L2_without_ocr(capsys, monkeypatch) -> None:
    # The flagship GhostCommit payload is rendered as VISIBLE PIXELS — no metadata,
    # no trailing bytes. On the base/no-OCR path L2 sees nothing, so this is CLEAN
    # and exits 0. WITH the `[ocr]` extra installed, L3 OCRs the pixels and this same
    # image verdicts POISON (basis `ocr → follow+exfil`) — documenting that OCR is
    # what closes this specific case. Force the no-OCR path (stub `ocr_text` → "") so
    # the assertion is deterministic whether or not tesseract is installed here.
    monkeypatch.setattr(imagescan, "ocr_text", lambda _d: "")
    rc = cli.main(["scan-image", str(_FIX / "build_spec_payload.png")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLEAN" in out
    _assert_no_payload(out)


def test_scan_image_missing_file_errors_cleanly(capsys) -> None:
    rc = cli.main(["scan-image", str(_FIX / "does_not_exist.png")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "does_not_exist" in err or "not" in err.lower()
