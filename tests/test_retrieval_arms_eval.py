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

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "retrieval_arms_eval",
    Path(__file__).resolve().parent.parent / "scripts" / "retrieval_arms_eval.py",
)
assert _SPEC and _SPEC.loader
arms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arms)


def _recall(card: dict, k: int) -> float:
    """The RANKER's recall — genuine hits only. The arm comparison is meaningless under the
    fallback reading: an arm that improves enough to stop the fallback firing loses recall
    by it."""
    return float(card["ranker"]["recall_at"][k])


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
            assert card["oos_pass_rate"]["ranker"] == 1.0


def test_the_report_never_quotes_a_recall_without_saying_which_reading() -> None:
    """The headline defect: `after_fix` counted never-empty-fallback positions as hits, and
    it was the number every report printed. Two things keep it from recurring — the arms
    report labels its figures as the ranker's and shows the fallback reading beside them,
    and the ambiguous key itself no longer resolves."""
    from gecko.evaluate import AmbiguousMetric

    results = arms.run()
    text = arms.format_report(results)
    assert "RANKER's — genuine hits only" in text
    assert "fallback-only tasks" in text and "r@20 if fallback counted" in text
    card = results["txodds"]["arms"]["B"]
    with pytest.raises(AmbiguousMetric):
        card["after_fix"]


def test_the_dense_gate_is_decided_on_the_ranker_not_the_fallback() -> None:
    """A card the two readings STRADDLE the gate on: the ranker finds nothing (recall@3
    0.00, gate fires) while every gold op is present as a fallback (1.00, gate would not
    fire). Built rather than measured, because no committed set happens to straddle 0.8 —
    and a gate test that both readings answer the same way proves nothing."""
    from dataclasses import dataclass

    from gecko.evaluate import GoldenTask, evaluate_golden

    @dataclass(frozen=True)
    class _Hit:
        name: str
        is_fallback: bool

    class _AllFallback:
        def search_scored(self, query: str, limit: int) -> list[_Hit]:
            return [_Hit("gold", True)]

    tasks = [
        GoldenTask(goal=f"q{i}", expect_ops=("gold",), archetype="keyword_echo")
        for i in range(4)
    ]
    card = evaluate_golden(_AllFallback(), tasks, limit=30)
    assert arms._ranker_recall(card, 3) == 0.0
    assert arms._fallback_recall(card, 3) == 1.0  # the readings straddle GATE_RECALL3
    assert arms.gate_fires(card, 51) is True


def test_the_txodds_readings_really_do_diverge() -> None:
    # Guards the above against the harness quietly ceasing to produce fallbacks at all:
    # on txodds 5 of 12 positives are fallback-only, so ranker < with-fallback at depth 20.
    card = arms.run()["txodds"]["arms"]["B"]
    assert card["n_via_fallback"] > 0
    assert arms._ranker_recall(card, 20) < arms._fallback_recall(card, 20)


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


# --- evidence: the control that must light up before these numbers are readable ---------


def test_every_set_carries_the_evidence_that_its_numbers_are_readable() -> None:
    """Each set's card names its denominators, its join coverage and its corpus digest.

    Formerly only the corpus digest was stamped (`_fixture_rev`); the join that decides
    whether recall CAN be non-zero was never checked at all.
    """
    for name, r in arms.run().items():
        sentence = r["evidence"]
        assert f"usable_ops={r['n']}" in sentence, name
        assert r["fixture"]["rev"] in sentence, name
        assert "join coverage 100%" in sentence, name
        assert "createTransferIntent" in sentence, f"{name}: the control is unnamed"


def test_a_gold_op_that_does_not_join_the_surface_refuses_the_run() -> None:
    """The wrong-key join: gold names that match no usable tool make recall 0 for a wiring
    reason, and a 0 that could not have come out otherwise is not a finding."""
    from gecko.evidence import Uninterpretable

    with pytest.raises(Uninterpretable) as excinfo:
        arms._set_evidence(
            "birdeye",
            usable={"defi_price", "token_overview"},
            gold={"birdeye:defi_price", "birdeye:token_overview"},
            positive_tasks=40,
            task_file=arms.GOLDEN / "birdeye_tasks.jsonl",
        )
    assert excinfo.value.reason == "join_shortfall"


def test_arms_wired_to_the_same_tokenizer_are_refused_before_being_differenced(
    monkeypatch,
) -> None:
    """A/B differ ONLY by `catalog._tokens`. Wire the baseline arm to the shipped tokenizer
    and the comparison becomes a value differenced with itself — which is what a silently
    failed monkeypatch looks like, and it must not report as "no lift"."""
    from gecko.evidence import Uninterpretable

    monkeypatch.setattr(arms, "_baseline_tokens", arms._SHIPPED_TOKENS)
    with pytest.raises(Uninterpretable) as excinfo:
        arms.run()
    assert excinfo.value.reason == "arms_identical"


def test_the_tokenizer_arms_do_diverge_on_the_control_as_shipped() -> None:
    # The contrast arm for the test above: unpatched, the two tokenizers really do answer
    # the known positive differently, so that refusal is a guard and not a constant.
    answers = {arm: run() for arm, run in arms._tokenizer_control().arms.items()}
    assert answers["A"] != answers["B"]
    assert all(answers.values()), "neither arm may be silent on the control"
