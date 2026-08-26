"""`gecko.evidence` — the control that has to light up before a number is a result.

Each test below is a bug this repo actually shipped, written once instead of four times:
two "arms" that were the same tick, a join on the wrong key that read as a 7% finding, an
empty corpus that printed `0/0 = 0%`, and a gate cleared by a class nobody exercised.
"""

from __future__ import annotations

import pytest

from gecko.evidence import (
    Control,
    Joined,
    Uninterpretable,
    corpus_rev,
    rate,
    require_signal,
)


# --- denominators ----------------------------------------------------------------------


def test_an_empty_denominator_is_uninterpretable_not_a_negative_result() -> None:
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal("mint-role recall", denominators={"labeled_ops": 0})
    assert excinfo.value.reason == "empty_denominator"
    assert "labeled_ops" in str(excinfo.value)


def test_a_populated_denominator_measures_so_the_two_arms_can_diverge() -> None:
    """The contrast arm. Without it the test above proves only that something raised."""
    signal = require_signal("mint-role recall", denominators={"labeled_ops": 31})
    assert signal.denominators["labeled_ops"] == 31


def test_a_denominator_under_the_declared_floor_refuses_with_the_floor_named() -> None:
    # The tier gate's own rule: a class scored 2 times cannot clear a 0.95 precision floor
    # in any meaningful sense — 1/d resolution is coarser than the thing being claimed.
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "tier precision", denominators={"transfer_high_pred": 2}, floor=5
        )
    assert excinfo.value.reason == "below_floor"
    assert "5" in str(excinfo.value)
    assert (
        require_signal(
            "tier precision", denominators={"transfer_high_pred": 5}, floor=5
        ).floor
        == 5
    )


# --- the join the caller CLAIMED it made -----------------------------------------------


def test_a_join_on_the_wrong_key_refuses_and_shows_both_key_shapes() -> None:
    """The 7%-finding bug: 40 labels, 3 matches, and the number read as a product result.

    The refusal has to print a sample of BOTH sides, because "coverage 0.07" is what the
    bug looked like the first time and it was mistaken for a finding for a fortnight.
    """
    claimed = [f"privy:op{i}" for i in range(40)]
    matched = ["privy:op0", "privy:op1", "privy:op2"]
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "tier recall",
            denominators={"scored": 3},
            joined=Joined("labels->ops", claimed=claimed, matched=matched),
        )
    assert excinfo.value.reason == "join_shortfall"
    text = str(excinfo.value)
    assert "37" in text, "the refusal must say how many keys did not join"
    assert "privy:op" in text, "the refusal must show the key shape on both sides"


def test_a_complete_join_passes_and_carries_its_coverage() -> None:
    keys = [f"privy:op{i}" for i in range(40)]
    signal = require_signal(
        "tier recall",
        denominators={"scored": 40},
        joined=Joined("labels->ops", claimed=keys, matched=keys),
    )
    assert signal.coverage == 1.0


def test_a_partial_join_is_allowed_only_when_the_caller_declares_the_shortfall() -> (
    None
):
    claimed = [f"op{i}" for i in range(10)]
    signal = require_signal(
        "recall",
        denominators={"scored": 8},
        joined=Joined(
            "gold->usable", claimed=claimed, matched=claimed[:8], min_coverage=0.8
        ),
    )
    assert signal.coverage == pytest.approx(0.8)
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "recall",
            denominators={"scored": 7},
            joined=Joined(
                "gold->usable", claimed=claimed, matched=claimed[:7], min_coverage=0.8
            ),
        )
    assert excinfo.value.reason == "join_shortfall"


def test_a_join_that_claimed_nothing_is_an_empty_denominator() -> None:
    # An empty corpus is not a negative result — including when it arrives as a join.
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "recall", denominators={"scored": 0}, joined=Joined("g->u", [], [])
        )
    assert excinfo.value.reason == "empty_denominator"


def test_matches_the_join_never_claimed_are_refused_as_a_key_mismatch() -> None:
    """Matching MORE than was claimed means the two sides were not the same population."""
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "recall",
            denominators={"scored": 3},
            joined=Joined("g->u", claimed=["a", "b"], matched=["a", "b", "c"]),
        )
    assert excinfo.value.reason == "join_shortfall"
    assert "c" in str(excinfo.value)


# --- the control: one known positive, through the same path ----------------------------


def test_a_control_that_does_not_light_up_makes_the_whole_run_uninterpretable() -> None:
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "bm25 recall",
            denominators={"tasks": 26},
            control=Control("createTransferIntent", {"bm25": lambda: []}),
        )
    assert excinfo.value.reason == "control_silent"
    assert "createTransferIntent" in str(excinfo.value)


@pytest.mark.parametrize("silent", [None, [], {}, 0, 0.0, False, ""])
def test_every_shape_of_nothing_counts_as_silent(silent: object) -> None:
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "recall",
            denominators={"tasks": 9},
            control=Control("known", {"a": lambda: silent}),
        )
    assert excinfo.value.reason == "control_silent"


def test_a_control_that_lights_up_passes_and_is_recorded_on_the_signal() -> None:
    signal = require_signal(
        "bm25 recall",
        denominators={"tasks": 26},
        control=Control(
            "createTransferIntent", {"bm25": lambda: ["createTransferIntent"]}
        ),
    )
    assert signal.control_case == "createTransferIntent"
    assert "createTransferIntent" in signal.control_answers["bm25"]


# --- arms that can actually diverge ----------------------------------------------------


def test_two_arms_that_return_the_same_tick_are_refused_before_they_are_differenced() -> (
    None
):
    """The bug: a "controlled comparison" whose arms were wired to the same source.

    Both arms answer the known positive identically, so any difference this run reports
    between them is zero BY CONSTRUCTION and no amount of data would change it.
    """
    with pytest.raises(Uninterpretable) as excinfo:
        require_signal(
            "tokenizer lift",
            denominators={"tasks": 40},
            control=Control(
                "createTransferIntent",
                {
                    "baseline": lambda: {"createtransferintent"},
                    "shipped": lambda: {"createtransferintent"},
                },
            ),
        )
    assert excinfo.value.reason == "arms_identical"
    assert "baseline" in str(excinfo.value) and "shipped" in str(excinfo.value)


def test_arms_that_genuinely_diverge_on_the_known_positive_pass() -> None:
    signal = require_signal(
        "tokenizer lift",
        denominators={"tasks": 40},
        control=Control(
            "createTransferIntent",
            {
                "baseline": lambda: {"createtransferintent"},
                "shipped": lambda: {"create", "transfer", "intent"},
            },
        ),
    )
    assert set(signal.control_answers) == {"baseline", "shipped"}


# --- rates: never 1.0 or 0.0 out of nothing --------------------------------------------


def test_an_empty_set_yields_none_and_not_a_perfect_or_zero_score() -> None:
    assert rate(0, 0) is None, "0/0 is not 0% and not 100% — it is unmeasured"


def test_a_measured_zero_and_a_measured_one_are_real_results() -> None:
    # The score.py line: measured badly is NOT the same as unmeasurable.
    assert rate(0, 12) == 0.0
    assert rate(12, 12) == 1.0


def test_more_hits_than_the_denominator_is_a_wiring_bug_not_a_rate() -> None:
    with pytest.raises(ValueError):
        rate(13, 12)


# --- the corpus stamp ------------------------------------------------------------------


def test_a_repartitioned_corpus_changes_the_stamp_even_at_the_same_size(
    tmp_path,
) -> None:
    """The regression that was not one: near_dup recall 0.85 -> 0.67 was a re-bucketing.

    Same task count, different partition — so a size check alone cannot tell the two runs
    apart and only the digest can.
    """
    before = tmp_path / "tasks.jsonl"
    before.write_text('{"archetype": "near_dup"}\n{"archetype": "keyword_echo"}\n')
    first = corpus_rev(before)
    before.write_text('{"archetype": "keyword_echo"}\n{"archetype": "keyword_echo"}\n')
    second = corpus_rev(before)
    assert first.items == second.items == 2
    assert first.digest != second.digest


def test_the_signal_sentence_is_rendered_from_the_fields_it_measured(tmp_path) -> None:
    """A number's qualifications get copied out of a document; a rendered sentence cannot
    drift from the fields a test asserts (the `fcc_eval.ApiLift.sentence` rule)."""
    corpus = tmp_path / "tasks.jsonl"
    corpus.write_text('{"a": 1}\n{"a": 2}\n')
    signal = require_signal(
        "bm25 recall@3",
        denominators={"tasks": 26, "usable_ops": 89},
        joined=Joined("gold->usable", claimed=["a"], matched=["a"]),
        control=Control(
            "createTransferIntent", {"bm25": lambda: ["createTransferIntent"]}
        ),
        corpus=[corpus_rev(corpus, name="birdeye_tasks")],
    )
    text = signal.sentence()
    assert "bm25 recall@3" in text
    assert "tasks=26" in text and "usable_ops=89" in text
    assert "birdeye_tasks" in text and signal.corpus[0].digest in text
    assert "createTransferIntent" in text
