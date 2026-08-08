"""The OCR engine seam: self-contained install, vendored language data, blinding canary.

Three things are locked here.

**1. The vendored language data is the bytes we think it is.** ``eng.traineddata`` is a
4 MB opaque binary that decides what the scanner can read. Nobody reviews it in a diff.
The hash assertion is the only thing standing between "we vendored Google's model" and
"we vendored whatever was in the working tree that day".

**2. The blinding canary.** This is the important one, and it is the only control in
the repo that detects silent model compromise at all.

    An OCR model is not human-reviewable. A weight swap looks like a binary diff and
    reads like noise. An attacker who can alter the traineddata does not need to touch
    a single line of Python to blind the scanner: make the model return empty text (or
    text with the trigger phrases dropped) and every rendered attack sails through as
    CLEAN, with no error, no exception, and no coverage gap reported — because the
    channel WAS read, it just came back innocent.

    Nothing else in the test suite would notice. Every other imagescan test injects a
    fake OCR seam, so they all keep passing against a blinded model. The recall corpus
    would catch it, but only where it overlaps the canary.

    So: OCR the real flagship GhostCommit payload and assert BOTH rules still fire. A
    build failure is the only signal we would ever get.

**3. Graceful degradation still degrades.** The fail-closed contract (``None`` means
"could not read", never "read it, saw nothing") must survive the engine swap.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gecko import imagescan

FIXTURES = Path(__file__).parent / "fixtures" / "imagescan"

#: Set in CI. Turns "no OCR engine" from a skip into a FAILURE.
#:
#: This is the difference between a canary and a decoration. Locally, skipping is right:
#: a base install genuinely has no OCR and the suite should stay green. In CI it is
#: exactly backwards — if the engine fails to install, every canary below SKIPS, the run
#: goes green, and the control silently protects nothing. A skipped canary and a blinded
#: canary are indistinguishable from the outside, which is the failure mode the canary
#: exists to prevent.
REQUIRE_OCR = os.environ.get("GECKO_REQUIRE_OCR") == "1"

#: Recorded in gecko/tessdata/PROVENANCE.md. tessdata_fast eng.traineddata @ tag 4.1.0.
EXPECTED_TRAINEDDATA_SHA256 = (
    "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2"
)


def test_vendored_traineddata_matches_recorded_hash():
    """The vendored model is exactly the upstream blob PROVENANCE.md names.

    This is a supply-chain assertion, not a housekeeping one. The file is binary, 4 MB,
    and completely opaque to review; without a pinned hash, substituting it is an
    invisible change to what the security scanner is capable of seeing.
    """
    blob = imagescan.vendored_tessdata_dir() / "eng.traineddata"
    assert blob.is_file(), (
        "vendored eng.traineddata is missing — the [ocr] extra is not self-contained "
        "without it; see gecko/tessdata/PROVENANCE.md"
    )
    digest = hashlib.sha256(blob.read_bytes()).hexdigest()
    assert digest == EXPECTED_TRAINEDDATA_SHA256, (
        "vendored eng.traineddata does NOT match the recorded hash.\n"
        f"  expected {EXPECTED_TRAINEDDATA_SHA256}\n  actual   {digest}\n"
        "If this was a deliberate update, update PROVENANCE.md and re-run the recall "
        "corpus — a language-data change is a change to what the scanner can detect."
    )


def _ocr_working() -> bool:
    """Probe with the flagship payload itself: it is the one committed fixture whose
    text lives ONLY in pixels, so recovering anything from it proves L3 really ran."""
    probe = (FIXTURES / "build_spec_payload.png").read_bytes()
    return bool(imagescan.ocr_text(probe).strip())


_OCR_WORKING = _ocr_working()

requires_ocr = pytest.mark.skipif(
    not _OCR_WORKING, reason="no working OCR engine installed"
)


def test_ocr_engine_is_actually_installed_in_ci():
    """With ``GECKO_REQUIRE_OCR=1``, a missing engine FAILS instead of skipping.

    Guards the canary against its own most likely failure mode. If ``tesserocr`` stops
    publishing a wheel, or the vendored language data goes missing from the built
    package, the canary tests below would quietly skip and CI would stay green while the
    pixel channel went completely unmonitored. This turns that into a red build.
    """
    if not REQUIRE_OCR:
        pytest.skip("GECKO_REQUIRE_OCR not set (local base install)")
    assert _OCR_WORKING, (
        "GECKO_REQUIRE_OCR=1 but no working OCR engine — the blinding canary would "
        "SKIP and CI would go green with the pixel channel unmonitored. Check that the "
        "[ocr] extra installed and that gecko/tessdata/eng.traineddata shipped."
    )
    assert imagescan.ocr_backend() == "tesserocr", (
        "expected the self-contained tesserocr backend in CI; falling back to "
        "pytesseract means the wheel did not install and the extra is not "
        f"self-contained on this platform (got {imagescan.ocr_backend()!r})"
    )


@requires_ocr
def test_blinding_canary_flagship_payload_still_fires_both_rules():
    """THE BLINDING CANARY. OCR the real GhostCommit image; both rules must fire.

    ``build_spec_payload.png`` is clean at L2 — no metadata, no trailing bytes — so the
    ONLY way it produces a verdict is by reading the rendered pixels. That makes it the
    perfect canary: if the OCR engine or its language data is ever compromised,
    degraded, or silently swapped, this image goes CLEAN and this test fails.

    Asserting the RULES rather than the recovered text is deliberate. Exact OCR output
    varies across tesseract builds and language-data versions (proven: 5.3.4 and 5.5.1
    return slightly different text but identical basis). Pinning the text would make
    this test flaky and it would get weakened. Pinning the basis pins the security
    property: this payload is still detected, on the pixel channel, for both reasons.
    """
    data = (FIXTURES / "build_spec_payload.png").read_bytes()
    verdict = imagescan.scan_image(data)
    basis = set(verdict.basis)

    assert "ocr → exfil_encoded_target" in basis, (
        "BLINDING CANARY FAILED: the GhostCommit exfil signature (secret source + "
        "numeric encoding) is no longer detected in the rendered pixels.\n"
        f"basis was: {sorted(basis)}\n"
        "Suspect the OCR engine or the vendored language data. This image carries NO "
        "metadata and NO trailing bytes — OCR is the only way it is ever caught."
    )
    assert "ocr → follow_rendered_instructions" in basis, (
        "BLINDING CANARY FAILED: the follow-rendered-instructions signal is no longer "
        f"detected. basis was: {sorted(basis)}"
    )
    assert verdict.tier == "poison"
    assert "ocr" not in verdict.channels_unavailable


@requires_ocr
def test_canary_would_actually_fail_if_the_model_were_blinded():
    """Prove the canary is load-bearing rather than merely green.

    A control that passes is not evidence until you have seen it fail for the reason it
    exists. This simulates exactly the compromise the canary is designed to catch — a
    model that returns empty text while reporting success, so the channel counts as READ
    and no coverage gap is raised — and asserts the verdict collapses to clean.

    Without this, a canary that passes because the assertions are vacuous is
    indistinguishable from one that passes because the scanner works.
    """
    data = (FIXTURES / "build_spec_payload.png").read_bytes()

    blinded = imagescan.scan_image(data, ocr=lambda _d: "")
    assert blinded.basis == (), "expected a blinded model to produce no basis"
    assert blinded.tier == "clean"
    # The dangerous part: a blinded model reports the channel as successfully READ, so
    # coverage looks complete and the operator gets an unqualified CLEAN.
    assert "ocr" not in blinded.channels_unavailable

    real = imagescan.scan_image(data)
    assert real.tier == "poison", "canary is vacuous — the real path does not fire"


def test_missing_engine_is_reported_as_unread_not_as_clean():
    """Fail-closed contract survives the swap: ``None`` != ``""``.

    ``None`` (could not read) and ``""`` (read it, no text) must stay distinguishable.
    Collapsing them is the original R9 bug: it made a base install report the flagship
    payload as CLEAN.
    """
    data = (FIXTURES / "build_spec_payload.png").read_bytes()
    verdict = imagescan.scan_image(data, ocr=lambda _d: None)
    assert "ocr" in verdict.channels_unavailable
    assert verdict.tier != "poison"


def test_ocr_text_never_raises_on_undecodable_bytes():
    """The public seam is a hardened parser of untrusted input; it degrades, never throws."""
    assert imagescan.ocr_text(b"not an image at all") == ""
    assert imagescan.ocr_text(b"") == ""


@requires_ocr
def test_engine_reports_which_backend_served_the_read():
    """The active backend is observable, so an operator can tell a self-contained
    install from one leaning on a system binary. Diagnostics only — never a verdict
    input, and it must never leak a filesystem path (control plane)."""
    name = imagescan.ocr_backend()
    assert name in {"tesserocr", "pytesseract", None}
    assert name is not None
    assert "/" not in name and "\\" not in name
