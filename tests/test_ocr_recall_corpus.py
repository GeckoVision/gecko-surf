"""The L3 OCR **recall corpus** — measured evidence for the OCR engine choice.

Why this file exists
--------------------
Every other imagescan test injects a fake OCR seam (``scan_image(ocr=...)``) and so
proves the *wiring*: recovered text reaches the sanitizer and produces a verdict.
None of them prove the thing that actually decides whether the pixel channel is
covered — **can the real engine read the pixels at all?**

That makes the OCR engine an invisible dependency. Swap it, and no test moves, while
real-world recall silently changes. This corpus is the control for that: it runs the
committed fixtures through the REAL engine and asserts a measured, per-fixture
outcome.

It is deliberately two-sided. 16 attacks measure recall; 7 near-miss benign fixtures
measure precision. A "recall improvement" that lights up the benign half is a
regression, and this file will say so.

The four named residuals
------------------------
``atk_tiny``, ``atk_rotated``, ``atk_twocol`` and ``atk_base64`` are asserted to be
**MISSED**. That is not a lowered bar — it is the honest boundary of a BEST-EFFORT
control, written down as a test so it cannot drift silently. If one starts being
caught, this file fails, and the improvement gets recorded deliberately instead of
being quietly absorbed into a recall number nobody re-derives.

L3 OCR is defence-in-depth. It is NOT a guarantee that the pixel channel is closed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from gecko import imagescan

RECALL_DIR = Path(__file__).parent / "fixtures" / "imagescan" / "recall"


def _load_corpus():
    """Import the committed generator so the corpus definition has ONE source."""
    path = RECALL_DIR / "make_recall_corpus.py"
    spec = importlib.util.spec_from_file_location("imagescan_recall_corpus", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolves annotations via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


corpus_module = _load_corpus()
CORPUS = corpus_module.CORPUS
EXPECTED_RESIDUALS = corpus_module.EXPECTED_RESIDUALS

#: Measured on the committed fixtures. Asserted as an aggregate so a change in overall
#: recall is a single obvious diff, not something reconstructed by counting test names.
EXPECTED_ATTACKS_CAUGHT = 12
EXPECTED_ATTACK_TOTAL = 16
EXPECTED_BENIGN_TOTAL = 7


def _ocr_available() -> bool:
    """True when the real OCR engine can actually read pixels on this machine.

    Probes the seam with a REAL fixture rather than checking for an importable module:
    the ``[ocr]`` extra being installed is not proof the engine can decode anything
    (a missing language pack reads every image as empty). ``None`` means the channel
    could not be read at all — exactly the fail-closed signal the scanner uses.
    """
    probe = (RECALL_DIR / "atk_plain_injection.png").read_bytes()
    return bool(imagescan.ocr_text(probe).strip())


requires_ocr = pytest.mark.skipif(
    not _ocr_available(),
    reason="no working OCR engine ([ocr] extra + language data) — L3 not exercised",
)

ATTACKS = [f for f in CORPUS if f.kind == "attack"]
BENIGN = [f for f in CORPUS if f.kind == "benign"]


def _basis_for(fixture) -> list[str]:
    """Run one fixture through the REAL scan path (no injected OCR seam)."""
    data = (RECALL_DIR / fixture.filename).read_bytes()
    verdict = imagescan.scan_image(data)
    return sorted(verdict.basis)


def test_corpus_shape_is_what_the_measurement_claims():
    """Guard the denominator. A recall ratio is meaningless if fixtures can be added or
    dropped without notice, so the corpus composition is itself asserted."""
    assert len(ATTACKS) == EXPECTED_ATTACK_TOTAL
    assert len(BENIGN) == EXPECTED_BENIGN_TOTAL
    assert EXPECTED_RESIDUALS <= {f.name for f in ATTACKS}
    for fixture in CORPUS:
        assert (RECALL_DIR / fixture.filename).is_file(), (
            f"{fixture.filename} missing — regenerate with make_recall_corpus.py"
        )


@requires_ocr
@pytest.mark.parametrize("fixture", ATTACKS, ids=lambda f: f.name)
def test_attack_recall(fixture):
    """Each rendered attack either quarantines, or is a NAMED residual."""
    basis = _basis_for(fixture)
    if fixture.name in EXPECTED_RESIDUALS:
        assert basis == [], (
            f"{fixture.name} is a NAMED RESIDUAL that is now being CAUGHT ({basis}). "
            "This is good news — but record it deliberately: update EXPECTED_RESIDUALS "
            "and EXPECTED_ATTACKS_CAUGHT, and re-state the residual set in the "
            "GhostCommit doc. Do not silently absorb it."
        )
        return
    assert basis, (
        f"{fixture.name} produced NO basis — the rendered attack went undetected.\n"
        f"rationale: {fixture.rationale}\n"
        "Either the OCR engine regressed or a detection rule narrowed. Do not add this "
        "to EXPECTED_RESIDUALS to make the suite green."
    )


@requires_ocr
@pytest.mark.parametrize("fixture", BENIGN, ids=lambda f: f.name)
def test_benign_precision(fixture):
    """No near-miss benign image may quarantine. Precision is half the measurement."""
    basis = _basis_for(fixture)
    assert basis == [], (
        f"{fixture.name} FALSE POSITIVE: {basis}\n"
        f"rationale: {fixture.rationale}\n"
        "A benign image that quarantines takes a whole real API surface offline."
    )


@requires_ocr
def test_measured_recall_matches_the_recorded_number():
    """The headline number, asserted as one fact.

    Locks the claim "the engine catches 12 of 16 rendered attacks with zero false
    positives" to something executable, so it is re-measured on every run rather than
    re-argued from memory in three months.
    """
    caught = [f.name for f in ATTACKS if _basis_for(f)]
    missed = sorted({f.name for f in ATTACKS} - set(caught))
    assert len(caught) == EXPECTED_ATTACKS_CAUGHT, f"recall moved; missed={missed}"
    assert set(missed) == set(EXPECTED_RESIDUALS)
    assert not [f.name for f in BENIGN if _basis_for(f)]


@requires_ocr
def test_twocol_residual_is_a_genuine_split_not_an_unread_image():
    """The two-column residual must fail for the RIGHT reason.

    ``atk_tiny`` and ``atk_rotated`` miss because OCR recovers nothing. ``atk_twocol``
    is different and more interesting: OCR reads the pixels *fine*, but in row-major
    order, interleaving the two columns so the directive is no longer contiguous. If
    this fixture ever started returning empty text it would still "pass" the residual
    assertion above while testing something else entirely, so the mechanism is pinned
    here: text IS recovered, the words ARE present, and the phrase is still broken.
    """
    data = (RECALL_DIR / "atk_twocol.png").read_bytes()
    recovered = imagescan.ocr_text(data)
    assert "ignore previous" in recovered.lower()
    assert "instructions" in recovered.lower()
    # ...yet the contiguous phrase never appears, because the right column is spliced in.
    assert "ignore previous instructions" not in recovered.lower()
    assert list(imagescan.scan_image(data).basis) == []
