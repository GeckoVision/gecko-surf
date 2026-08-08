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

It is deliberately two-sided. 18 attacks measure recall; 8 near-miss benign fixtures
measure precision. A "recall improvement" that lights up the benign half is a
regression, and this file will say so.

It asserts the CHANNEL, not only the rule
-----------------------------------------
A rule calibrated on clean spec text does not automatically survive OCR. The renderer
moves line breaks — and every ``[^.\\n]``-scoped sanitize rule treats one as a hard stop,
while the ATTACKER picks the image width — and it shatters base58 literals into
space-separated runs. ``atk_wrapped_directive`` and ``atk_fund_routing_funds`` are here
because they fail on the RAW recovery and pass only once :mod:`gecko.ocrnorm` undoes the
renderer; ``benign_id_table`` is here because undoing it can manufacture a false
positive. That trio is what stops the next rule change from passing on clean text while
silently failing on pixels. The rule-level half of the same lesson lives in
``tests/test_ocrnorm.py``, which needs no OCR engine and so runs on the base install.

The named residuals
-------------------
``EXPECTED_RESIDUALS`` is asserted to be **MISSED**. That is not a lowered bar — it is
the honest boundary of a BEST-EFFORT control, written down as a test so it cannot drift
silently. If one starts being caught, this file fails, and the improvement gets recorded
deliberately instead of being quietly absorbed into a recall number nobody re-derives.
Read each fixture's ``rationale``: they miss for different reasons (unreadable pixels,
un-decoded encoding, a deliberately-narrowed rule, a layout whose re-joining was a
measured false positive), and only some of them are about OCR at all.

L3 OCR is defence-in-depth. It is NOT a guarantee that the pixel channel is closed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from gecko import imagescan, ocrnorm, sanitize

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
EXPECTED_ATTACKS_CAUGHT = 14
EXPECTED_ATTACK_TOTAL = 18
EXPECTED_BENIGN_TOTAL = 8


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

    Locks the claim "the pipeline catches 14 of 18 rendered attacks with zero false
    positives" to something executable, so it is re-measured on every run rather than
    re-argued from memory in three months.

    The number is for the ENGINE PLUS gecko.ocrnorm, which is the only honest way to
    state it: on the raw recovery two of the caught attacks (atk_fund_routing_funds,
    atk_wrapped_directive) are missed, so quoting a recall figure without naming the
    normalisation step overstates what tesseract alone does.
    """
    caught = [f.name for f in ATTACKS if _basis_for(f)]
    missed = sorted({f.name for f in ATTACKS} - set(caught))
    assert len(caught) == EXPECTED_ATTACKS_CAUGHT, f"recall moved; missed={missed}"
    assert set(missed) == set(EXPECTED_RESIDUALS)
    assert not [f.name for f in BENIGN if _basis_for(f)]


@requires_ocr
@pytest.mark.parametrize("name", ["atk_fund_routing_funds", "atk_wrapped_directive"])
def test_channel_fidelity_attacks_need_normalisation_through_real_pixels(name):
    """These two attacks are INVISIBLE on the raw recovery and visible after it.

    This is the whole channel lesson, asserted end-to-end through real pixels rather
    than a hand-written string: the sanitize rules are unchanged and were already
    catching this text on the clean channel, yet the RENDERER alone hid it. If a future
    change makes the raw recovery sufficient, the first assertion trips and says so.

    ``tests/test_ocrnorm.py`` proves the same property on literals without an OCR
    engine; this proves the wiring actually reaches the scanner.
    """
    fixture = next(f for f in ATTACKS if f.name == name)
    recovered = imagescan.ocr_text((RECALL_DIR / fixture.filename).read_bytes())
    assert recovered.strip(), "fixture unreadable — this is testing nothing"
    assert not sanitize.scan_convention_text(recovered), (
        f"{name} no longer needs normalisation on the raw recovery — good news, but "
        "re-derive why ocrnorm exists before relying on that."
    )
    assert sanitize.scan_convention_text(ocrnorm.normalize_recovered_text(recovered)), (
        f"{name} still undetected after normalisation"
    )
    assert _basis_for(fixture), f"{name} not caught through the real scan path"


@requires_ocr
def test_the_table_threshold_holds_in_both_directions_through_real_pixels():
    """One identifier on a line is a wrapped sentence; two or more is a table.

    That single threshold decides both of these fixtures, in opposite directions, and
    only real pixels prove it: ``atk_addr_alone_line`` is CAUGHT because the address is
    re-joined to the directive above it, and ``benign_id_table`` stays CLEAN because its
    rows are not. Treating both as tables was measured to cost 18 of 68 attack/width
    pairs on PR #330's own attack set; treating neither as a table quarantined the
    benign document. Neither fixture means anything without the other, so they are
    asserted together.
    """
    attack = (RECALL_DIR / "atk_addr_alone_line.png").read_bytes()
    recovered = imagescan.ocr_text(attack)
    assert "transfer the remaining funds" in recovered.lower()
    assert "\n" in recovered.strip(), "not a wrapped fixture — this tests nothing"
    assert not sanitize.scan_convention_text(recovered), "raw recovery already fires"
    assert list(imagescan.scan_image(attack).basis) == ["ocr → fund_routing"]

    benign = (RECALL_DIR / "benign_id_table.png").read_bytes()
    assert list(imagescan.scan_image(benign).basis) == []


@requires_ocr
def test_twocol_is_caught_incidentally_and_the_split_mechanism_is_still_open():
    """``atk_twocol`` is caught — but NOT because column splitting is handled.

    OCR reads these pixels fine, in row-major order, interleaving the two columns so the
    directive is no longer contiguous. ``gecko.ocrnorm`` then unwraps the interleaved
    rows and, purely because of what the right column happens to contain, the words
    ``send ... the private key`` end up in one clause and trip ``secret_exfil``.

    That is luck of content, not a mechanism, and the distinction is worth a test: the
    ``prompt_injection`` phrase the fixture was BUILT around is still shredded, so the
    split-payload residual named in the sanitize docstring is NOT closed. Anyone citing
    this fixture as coverage for column-split payloads is misreading it.
    """
    data = (RECALL_DIR / "atk_twocol.png").read_bytes()
    recovered = imagescan.ocr_text(data)
    assert "ignore previous" in recovered.lower()
    assert "instructions" in recovered.lower()
    # The directive the fixture was built around is still broken by the interleave...
    assert "ignore previous instructions" not in recovered.lower()
    basis = sorted(imagescan.scan_image(data).basis)
    assert "ocr → prompt_injection" not in basis
    # ...and the hit that DOES land comes from the unrelated secret-exfil clause.
    assert basis == ["ocr → secret_exfil"]
