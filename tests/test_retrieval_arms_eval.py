"""The retrieval-arm comparison as a CI guard (retrieval spec §4).

Locks the MEASURED finding: BM25 (arm C) is a no-op on the small golden sets (surface-all
decouples rank from FCC there) but LIFTS recall on the first real >50-op set (privy, 159
ops) without regressing OOS — the exact condition the op-count gate was pre-committed for.
Also guards the mechanical contract: three sets, arms A/B/C run offline, the module-global
tokenizer is restored.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_arms_eval",
    Path(__file__).resolve().parent.parent / "scripts" / "retrieval_arms_eval.py",
)
assert _SPEC and _SPEC.loader
arms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arms)


def _recall(card: dict, k: int) -> float:
    return float(card["after_fix"]["recall_at"][k])


def test_covers_four_golden_sets_with_three_offline_arms() -> None:
    # Named explicitly rather than read from `arms.CASES`, which is the thing under test —
    # deriving the expectation from the code would make dropping a set invisible.
    results = arms.run()
    assert set(results) == {"txodds", "pegana", "privy", "birdeye"}
    for r in results.values():
        assert set(r["arms"]) == {"A", "B", "C"}


def test_privy_is_the_scale_gate_set() -> None:
    results = arms.run()
    assert results["privy"]["n"] == 159, (
        "privy must comprehend to the >50-op scale-gate set"
    )


def test_bm25_no_longer_leads_the_overlap_baseline_on_the_large_surface() -> None:
    """RE-MEASURED. This test used to assert BM25 (arm C) beat the overlap baseline
    (arm B) by >= 0.10 at recall@3 on the 159-op set — the payoff the op-count gate was
    pre-committed for, and true when it was written (B: r@1 0.53 / r@3 0.67 / MRR 0.646
    vs C: 0.67 / 0.80 / 0.746).

    Adding morphological folding + the genericity floor to the overlap arm moved B to
    r@1 0.73 / r@3 0.80 / MRR 0.794: it now TIES BM25 at recall@3 and LEADS it at
    recall@1 and MRR. The old assertion is not a regression that needs fixing — it is a
    claim the new measurement falsifies, so it is replaced by the claim the numbers now
    support. What follows from it (whether the >50-op BM25 adoption gate still has a
    premise) is an architecture call for staff-engineer, not something this test decides.
    """
    privy = arms.run()["privy"]["arms"]
    assert _recall(privy["B"], 1) >= _recall(privy["C"], 1), (
        "the folded+floored overlap arm must not fall behind BM25 at recall@1"
    )
    assert _recall(privy["B"], 3) >= _recall(privy["C"], 3), (
        "…nor at recall@3 — if BM25 pulls ahead again, re-open the adoption gate"
    )


def test_bm25_preserves_oos_pass_rate_everywhere() -> None:
    # A stronger ranker must not manufacture confident false positives on out-of-scope intents
    # (the lexical-anchored confidence floor). OOS pass-rate stays 1.00 across every arm/set.
    for r in arms.run().values():
        for card in r["arms"].values():
            assert card["oos_pass_rate"]["after_fix"] == 1.0


def test_bm25_flat_on_small_sets() -> None:
    # Below the 50-op gate BM25 buys nothing over the overlap baseline (the accepted null).
    results = arms.run()
    for name in ("txodds", "pegana"):
        a = results[name]["arms"]
        for k in (1, 3, 5):
            assert _recall(a["C"], k) == _recall(a["A"], k)


def test_harness_restores_the_shipped_tokenizer() -> None:
    from gecko import catalog

    before = catalog._tokens
    arms.run()
    assert catalog._tokens is before, "harness must not leave the module global patched"
