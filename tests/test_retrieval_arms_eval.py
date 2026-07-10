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


def test_covers_three_golden_sets_with_three_offline_arms() -> None:
    results = arms.run()
    assert set(results) == {"txodds", "pegana", "privy"}
    for r in results.values():
        assert set(r["arms"]) == {"A", "B", "C"}


def test_privy_is_the_scale_gate_set() -> None:
    results = arms.run()
    assert results["privy"]["n"] == 159, (
        "privy must comprehend to the >50-op scale-gate set"
    )


def test_bm25_lifts_recall_on_the_large_surface() -> None:
    # The load-bearing measured claim: on the 159-op set BM25 beats the overlap baseline at
    # recall@3, and never regresses recall@1/@3 — the payoff the gate predicted.
    privy = arms.run()["privy"]["arms"]
    assert _recall(privy["C"], 3) >= _recall(privy["B"], 3) + 0.10
    assert _recall(privy["C"], 1) >= _recall(privy["B"], 1)


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
