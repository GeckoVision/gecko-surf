"""retrieval_eval — the misrank-aware eval for the lexical-vs-semantic gate.

The production on_miss seam fires only at score 0, so misranks (gold wired but
ranked below top-k) — the only real evidence FOR a semantic retriever — never
log. These tests pin the closed miss-cause vocabulary, the golden-set replay,
the aggregate metrics, and the control-plane rule that no record ever carries
intent text.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from gecko.retrieval_eval import (
    GoldenSetError,
    classify_miss,
    default_golden_text,
    evaluate_golden,
    format_report,
    load_golden,
)

# --- the committed golden set ----------------------------------------------------


def test_committed_golden_set_loads_and_is_small() -> None:
    rows = load_golden(default_golden_text())
    # Small, reviewable — a fixture, not a corpus. The ceiling moved 40 -> 60 when the
    # out-of-scope block was widened from 4 rows to 12: the original four carry NO term
    # that names a wired program or instruction, so they only ever exercised the
    # score-0 path and the floor's `named` branch was never measured at all.
    assert 15 <= len(rows) <= 60
    # it covers every wired program AND deliberately-out-of-scope intents
    programs = {r.gold_program for r in rows}
    assert {"pumpfun", "meteora", "ore", "metadao_ico", None} <= programs


def test_out_of_scope_rows_exercise_the_named_branch_of_the_floor() -> None:
    """The out-of-scope block must contain intents that DO carry an identity term.

    This pins a property of the FIXTURE, not a verdict about the ranker: an
    out-of-scope set whose rows share no term with any card can only ever prove the
    score-0 path, and would report a perfect floor while the `named` branch — one
    matched instruction name is sufficient, on its own, for a RUNNABLE start — sits
    entirely unmeasured. What these rows currently do is reported in the eval, not
    asserted here; asserting a rate would freeze today's (bad) number as the contract.
    """
    from gecko.find_start import _card_terms, _identity_terms, _query_tokens
    from gecko.find_start import _wired_cards

    identity = set()
    for card in _wired_cards():
        if card.kind == "start":
            identity |= _identity_terms(card) & _card_terms(card)
    with_identity = [
        r
        for r in load_golden(default_golden_text())
        if r.gold_program is None and (_query_tokens(r.intent) & identity)
    ]
    assert len(with_identity) >= 8, (
        "the out-of-scope block must keep at least 8 rows carrying an identity term, "
        "or the floor's `named` branch stops being measured"
    )


def test_golden_set_includes_unwired_golds_for_coverage_gap_rows() -> None:
    rows = load_golden(default_golden_text())
    # pump `sell` was wired this sprint (it used to sit here alongside add_liquidity);
    # meteora add_liquidity is still unwired and keeps the coverage_gap cause honest —
    # the set must always carry at least one gold nothing serves, or the eval stops
    # being able to tell "we retrieve badly" from "we haven't built it".
    unwired = [r for r in rows if r.gold_instruction == "add_liquidity"]
    assert unwired


# --- golden-set validation (committed data still fails loud) ---------------------


def test_load_golden_rejects_bad_json() -> None:
    with pytest.raises(GoldenSetError, match="line 1"):
        load_golden("not json\n")


def test_load_golden_rejects_missing_intent() -> None:
    with pytest.raises(GoldenSetError, match="'intent'"):
        load_golden('{"gold_program": "pumpfun", "gold_instruction": "buy"}\n')


def test_load_golden_rejects_instruction_without_program() -> None:
    with pytest.raises(GoldenSetError, match="gold_instruction without"):
        load_golden(
            '{"intent": "x y z", "gold_program": null, "gold_instruction": "buy"}'
        )


def test_load_golden_rejects_empty_set() -> None:
    with pytest.raises(GoldenSetError, match="empty"):
        load_golden("\n\n")


# --- the closed miss-cause vocabulary (the branch table, pure) -------------------


def test_classify_no_content_tokens_wins_first() -> None:
    cause = classify_miss(
        has_content_tokens=False,
        out_of_scope=True,
        gold_wired=False,
        no_start=True,
        gold_rank=None,
        gold_overlap=0,
        k=3,
    )
    assert cause == "no_content_tokens"


def test_classify_out_of_scope_rejection_is_a_hit() -> None:
    cause = classify_miss(
        has_content_tokens=True,
        out_of_scope=True,
        gold_wired=False,
        no_start=True,
        gold_rank=None,
        gold_overlap=0,
        k=3,
    )
    assert cause == "hit"


def test_classify_out_of_scope_clearing_the_floor_is_a_false_accept() -> None:
    cause = classify_miss(
        has_content_tokens=True,
        out_of_scope=True,
        gold_wired=False,
        no_start=False,
        gold_rank=None,
        gold_overlap=0,
        k=3,
    )
    assert cause == "false_accept"


def test_classify_unwired_gold_is_a_coverage_gap_never_flip_evidence() -> None:
    cause = classify_miss(
        has_content_tokens=True,
        out_of_scope=False,
        gold_wired=False,
        no_start=False,
        gold_rank=None,
        gold_overlap=0,
        k=3,
    )
    assert cause == "coverage_gap"


def test_classify_gold_within_k_is_a_hit() -> None:
    cause = classify_miss(
        has_content_tokens=True,
        out_of_scope=False,
        gold_wired=True,
        no_start=False,
        gold_rank=3,
        gold_overlap=2,
        k=3,
    )
    assert cause == "hit"


def test_classify_zero_overlap_is_a_vocabulary_gap() -> None:
    cause = classify_miss(
        has_content_tokens=True,
        out_of_scope=False,
        gold_wired=True,
        no_start=True,
        gold_rank=None,
        gold_overlap=0,
        k=3,
    )
    assert cause == "vocabulary_gap"


def test_classify_wired_with_overlap_below_k_is_a_misrank() -> None:
    # THE gap this module exists for: the gold IS wired, terms DO overlap,
    # lexical ranking still buries it — evidence the old seam never saw.
    cause = classify_miss(
        has_content_tokens=True,
        out_of_scope=False,
        gold_wired=True,
        no_start=False,
        gold_rank=4,
        gold_overlap=1,
        k=3,
    )
    assert cause == "misrank"


# --- the golden replay against the real router -----------------------------------


def test_evaluate_golden_pins_the_showcase_rows() -> None:
    report = evaluate_golden()
    by_intent = {o.row.intent: o for o in report.rows}

    showcase = by_intent["buy this token on pump and hold it"]
    assert showcase.cause == "hit"
    assert showcase.gold_rank == 1
    assert showcase.top1_name == "pumpfun/buy"

    # A true paraphrase with concrete mints the gold card never names: the gold is
    # wired, the vocabulary just doesn't reach it — real flip evidence.
    #
    # This row used to assert floor == "start", pinning a DEFECT as expected behaviour:
    # metadao/fund's "usdc" vocabulary cleared the old floor and was served as a plan to
    # RUN, with top1 != gold. A single shared noun that names neither a program nor an
    # action is not evidence, so the floor now demotes it to a guess. The vocabulary gap
    # is unchanged and still countable — what changed is that we no longer hand back a
    # wrong runnable start while we have it.
    paraphrase = by_intent["convert usdc to bonk"]
    assert paraphrase.cause == "vocabulary_gap"
    assert paraphrase.floor == "guess"
    assert paraphrase.top1_name != "meteora/swap"

    # the fund rows wired this sprint route to the new start (was a coverage gap)
    fund = by_intent["fund a launchpad token launch"]
    assert fund.cause == "hit"
    assert fund.top1_name == "metadao_ico/fund"

    # the sell rows wired this sprint route to the new start (both were coverage gaps)
    sell = by_intent["sell my pump tokens back to the curve"]
    assert sell.cause == "hit"
    assert sell.top1_name == "pumpfun/sell"
    # …including the paraphrase that shares no verb with the card
    dump = by_intent["dump this memecoin position before it rugs"]
    assert dump.cause == "hit"
    assert dump.top1_name == "pumpfun/sell"

    # meteora add_liquidity is still unwired: a coverage gap, which argues for wiring —
    # not for vectors. The eval must keep at least one of these to stay honest.
    unwired = by_intent["add liquidity to a meteora pool"]
    assert unwired.cause == "coverage_gap"

    nonsense = by_intent["flumbuzzle the quantum wombat"]
    assert nonsense.cause == "hit"  # the floor honestly rejected it
    assert nonsense.gold_rank is None


def test_evaluate_golden_aggregates_are_consistent() -> None:
    report = evaluate_golden()
    assert 0.0 <= report.recall_at_1 <= report.recall_at_3 <= 1.0
    assert 0.0 <= report.mrr <= 1.0
    assert report.recall_at_1 <= report.mrr  # rank-1 hits bound MRR from below
    assert sum(report.causes.values()) == len(report.rows)
    assert report.semantic_evidence == report.causes.get(
        "misrank", 0
    ) + report.causes.get("vocabulary_gap", 0)
    # coverage gaps and out-of-scope rows are EXCLUDED from the recall denominator
    assert report.scoreable == len(
        report.rows
    ) - report.out_of_scope - report.causes.get("coverage_gap", 0)


def test_records_never_carry_intent_text() -> None:
    """Control plane: the categorical records hold names/scores/ranks only."""
    report = evaluate_golden()
    dumped = json.dumps([asdict(o.record) for o in report.rows])
    for intent_word in ("wombat", "lisbon", "memecoin", "aping", "weather"):
        assert intent_word not in dumped


def test_below_floor_rows_are_marked_guess_floor() -> None:
    """The below-floor acceptance seam: a caller proceeding on a GUESS is
    countable via floor='guess' on the record."""
    report = evaluate_golden()
    for outcome in report.rows:
        record = outcome.record
        assert record.floor in {"start", "guess"}
        if record.floor == "guess":
            # No RUNNABLE candidate survived the floor. A `surface` card may still ride
            # along — it says "this program is plausibly relevant, start from its derive
            # tools", which is a hedge, not a plan, so it carries none of the risk the
            # floor exists to prevent.
            assert all(c.kind != "start" for c in record.top_candidates)


def test_evaluate_golden_accepts_a_custom_fixture(tmp_path: Path) -> None:
    golden = tmp_path / "golden.jsonl"
    golden.write_text(
        json.dumps(
            {
                "intent": "swap sol for usdc",
                "gold_program": "meteora",
                "gold_instruction": "swap",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = evaluate_golden(golden)
    assert report.scoreable == 1
    assert report.recall_at_1 == 1.0
    assert report.mrr == 1.0


# --- rendering + CLI -------------------------------------------------------------


def test_format_report_prints_the_verdict_and_framing() -> None:
    text = format_report(evaluate_golden())
    assert "recall@1" in text
    assert "semantic-flip evidence:" in text
    assert "not auto-flipping anything" in text
    assert "argue for wiring more programs" in text  # the coverage-gap framing


def test_cli_eval_retrieval_default_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    from gecko.providers.cli import eval_retrieval_main

    assert eval_retrieval_main([]) == 0
    out = capsys.readouterr().out
    assert "semantic-flip evidence:" in out
    assert "MRR" in out


def test_cli_eval_retrieval_bad_path_fails_loud(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gecko.providers.cli import eval_retrieval_main

    assert eval_retrieval_main(["--golden", "/nonexistent/golden.jsonl"]) == 2
    assert "eval-retrieval:" in capsys.readouterr().err


# --- R2: the measured floors, and the false-accept split by branch ----------------

# The tree this change landed on measured recall@1 0.7576 (25/33) and recall@3 0.9091
# (30/33) with 8/12 false accepts. Those are the ACCEPTANCE FLOORS, fixed in advance:
# a retrieval change may raise them and may not lower them. They are asserted as
# inequalities, not equalities — pinning today's exact number would make every future
# improvement a test failure, and pinning it as a ceiling is how a tuning pass quietly
# becomes a contract.
RECALL_AT_1_FLOOR = 0.7576
RECALL_AT_3_FLOOR = 0.9091
FALSE_ACCEPT_CEILING = 8


def test_the_measured_retrieval_floors_hold() -> None:
    report = evaluate_golden()
    assert report.recall_at_1 >= RECALL_AT_1_FLOOR, (
        f"recall@1 {report.recall_at_1:.4f} over {report.scoreable} wired-gold rows "
        f"regressed below the {RECALL_AT_1_FLOOR} this change landed on"
    )
    assert report.recall_at_3 >= RECALL_AT_3_FLOOR, (
        f"recall@3 {report.recall_at_3:.4f} over {report.scoreable} wired-gold rows "
        f"regressed below the {RECALL_AT_3_FLOOR} this change landed on"
    )
    assert report.false_accepts <= FALSE_ACCEPT_CEILING, (
        f"{report.false_accepts}/{report.out_of_scope} out-of-scope intents cleared "
        f"the floor (was {FALSE_ACCEPT_CEILING}/{report.out_of_scope})"
    )


def test_false_accepts_are_reported_split_by_floor_branch() -> None:
    """A single bundled false-accept figure hides WHICH branch admitted each row, and
    the two branches carry different risk: ``named`` needs no corroboration at all.

    Both figures carry their denominator by construction — ``BranchCount`` has no
    rendering that omits it.
    """
    from gecko.retrieval_eval import (
        false_accepts_admitted_by_corroboration,
        false_accepts_admitted_by_name,
    )

    report = evaluate_golden()
    by_name = false_accepts_admitted_by_name(report)
    by_corroboration = false_accepts_admitted_by_corroboration(report)

    assert by_name.denominator == report.out_of_scope
    assert by_corroboration.denominator == report.out_of_scope
    assert str(by_name) == f"named={by_name.count}/{report.out_of_scope}"

    # Measured on the R1 out-of-scope block: `named` admits 8 of 12, `corroborated`
    # 2 of 12 (both of those are also named). The `named` branch is doing essentially
    # all of the damage — "buy a house" carries `buy`, which names BOTH pumpfun's
    # instruction and let_me_buy's api_id. That is a floor finding, not a ranker one.
    assert by_name.count == 8
    assert by_corroboration.count == 2
    assert by_name.count + by_corroboration.count >= report.false_accepts


def test_the_floor_refuses_exactly_the_rows_it_refused_before() -> None:
    """Pins WHAT the floor refuses, so any retrieval change that moves it fails loudly
    here rather than showing up as a quiet aggregate shift.

    These four out-of-scope intents share no term with any card, so they are refused
    on the score-0 path. The other eight DO carry an identity term and are admitted —
    that is the standing floor defect the R1 rows were authored to expose, recorded
    here as it is, not asserted as acceptable.
    """
    report = evaluate_golden()
    refused = {
        o.row.intent
        for o in report.rows
        if o.row.gold_program is None and not o.admissions
    }
    assert refused == {
        "flumbuzzle the quantum wombat",
        "what is the weather in lisbon",
        "send ten usdc to my friend's wallet",
        "stake sol with a validator",
    }
