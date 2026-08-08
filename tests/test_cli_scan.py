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


def test_scan_image_clean_arch_full_coverage_exit_zero(capsys, monkeypatch) -> None:
    """A clean image, every channel readable → a plain CLEAN and exit 0.

    R9 made this test state its install: the assertion used to be unconditional, but on a
    BASE install (no [ocr]/[imagescan] extras) this same image now exits
    ``_SCAN_INCOMPLETE_EXIT`` — correctly, because that install cannot read the pixel or
    deep-metadata channels of ANY image. Stubbing both readable pins the case this test is
    actually about: complete coverage must still produce an uncaveated pass. That is the
    false-positive guard — a scanner that caveats every result gets ignored.
    """
    monkeypatch.setattr(imagescan, "_read_rendered_text", lambda _d: "")
    monkeypatch.setattr(imagescan, "_deep_metadata_available", lambda: True)
    rc = cli.main(["scan-image", str(_FIX / "clean_arch.png")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLEAN" in out
    assert "could not scan" not in out


def test_scan_image_build_spec_without_ocr_is_INCOMPLETE_never_clean(
    capsys, monkeypatch
) -> None:
    """R9 REGRESSION (this test previously asserted the DEFECT — see the PR note).

    The flagship GhostCommit payload is rendered as VISIBLE PIXELS: no metadata, no
    trailing bytes. On a base install the rendered-pixel channel is UNREADABLE, so the
    scanner has no basis for any verdict on the one channel this attack class uses. It
    must NOT print CLEAN and must NOT exit 0 — a scan that could not run must not render
    as a scan that passed. It says so, names the channel, and exits non-zero.
    """
    monkeypatch.setattr(imagescan, "_read_rendered_text", lambda _d: None)
    rc = cli.main(["scan-image", str(_FIX / "build_spec_payload.png")])
    out = capsys.readouterr().out
    assert rc == cli._SCAN_INCOMPLETE_EXIT
    assert "CLEAN" not in out
    assert "INCOMPLETE" in out
    assert "ocr" in out
    _assert_no_payload(out)


def test_scan_image_incomplete_scan_still_reports_what_it_DID_find(
    capsys, monkeypatch
) -> None:
    """An unreadable channel must not swallow a real finding from a readable one. POISON
    outranks INCOMPLETE (a found attack is actionable now) and keeps the exit-2 contract,
    but the coverage gap is still printed — both facts are true and both are reported."""
    monkeypatch.setattr(imagescan, "_read_rendered_text", lambda _d: None)
    rc = cli.main(["scan-image", str(_FIX / "poison_exif.png")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "POISON" in out
    assert "could not scan" in out
    _assert_no_payload(out)


def test_scan_image_allow_missing_channels_is_informed_consent_not_silence(
    capsys, monkeypatch
) -> None:
    """The opt-out exists so the [ocr] extra is not de-facto mandatory — but it buys back
    the zero exit ONLY, never the silence. The caveat is still printed, so the operator
    who suppresses the exit code has still been told what was not scanned."""
    monkeypatch.setattr(imagescan, "_read_rendered_text", lambda _d: None)
    rc = cli.main(
        [
            "scan-image",
            str(_FIX / "build_spec_payload.png"),
            "--allow-missing-channels",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not scan" in out
    assert "ocr" in out


def test_scan_image_missing_file_is_INCOMPLETE_not_POISON(capsys) -> None:
    """An unreadable file exits ``_SCAN_INCOMPLETE_EXIT``, never the POISON code.

    Both are non-zero, so `scan && deploy` blocks either way — this is about which
    claim the exit code makes. Exit 2 says "I evaluated this artifact and it failed";
    a file that could not be opened was never evaluated at all. That is exactly the
    distinction exit 3 exists for, and mislabelling it would put a benign typo'd path
    in the same bucket as a live GhostCommit payload.
    """
    rc = cli.main(["scan-image", str(_FIX / "does_not_exist.png")])
    err = capsys.readouterr().err
    assert rc == 3
    assert "does_not_exist" in err or "not" in err.lower()


def test_scan_doc_missing_file_is_INCOMPLETE_not_POISON(capsys) -> None:
    # Same rule on the doc path — one convention, both entrypoints.
    rc = cli.main(["scan-doc", str(_FIX / "does_not_exist.md")])
    err = capsys.readouterr().err
    assert rc == 3
    assert "does_not_exist" in err or "not" in err.lower()


def test_scan_image_zero_channels_says_why_not_just_zero(capsys, monkeypatch) -> None:
    """A text-free image passes, and the output says WHY the count is zero.

    ``clean_arch.png`` is a synthetic PNG with no rendered text and no metadata, so
    every channel was readable and none carried text. The verdict is a correct CLEAN
    — but a bare "channels scanned: 0" under a pass reads as "looked at nothing,
    declared it fine", which is the one sentence a reader will (reasonably) not
    forgive. The count keeps its meaning; the line states what produced it.
    """
    monkeypatch.setattr(imagescan, "_read_rendered_text", lambda _d: "")
    monkeypatch.setattr(imagescan, "_deep_metadata_available", lambda: True)
    rc = cli.main(["scan-image", str(_FIX / "clean_arch.png")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CLEAN" in out
    assert "channels scanned: 0" in out
    # the disambiguation: every channel WAS read, none carried text
    assert "every channel was readable" in out
    assert "none carried text" in out
    assert "could not scan" not in out
