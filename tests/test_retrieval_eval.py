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

from gecko.find_start import ChainPlan, FindStartResult, StartPoint, candidate_name
from gecko.retrieval_eval import (
    GoldenRow,
    GoldenSetError,
    _gold_rank,
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


# --- _gold_rank: what counts as a served point -----------------------------------

_NO_CHAIN = ChainPlan(
    name="",
    status="not_evaluated",
    verdict="NOT_EVALUATED",
    steps=(),
    links=(),
    unresolved=(),
    note="fixture",
)


def _point(kind: str, program: str, instruction: str | None) -> StartPoint:
    """A minimal ranked point — only kind/program/instruction matter to _gold_rank."""
    return StartPoint(
        kind=kind,  # type: ignore[arg-type]
        program=program,
        program_id="Prog1111",
        instruction=instruction,
        next_tool=None,
        score=1,
        why=(),
        inputs=(),
        derive_plan=(),
        preludes=(),
        gaps=(),
        execute=None,
        serve="fixture",
        chain=_NO_CHAIN,
    )


def _result(*points: StartPoint, no_start: bool = False) -> FindStartResult:
    return FindStartResult(starts=points, catalog=(), no_start=no_start, note="fixture")


def test_gold_rank_does_not_credit_a_guess_that_matches_the_gold() -> None:
    """A guess is below the retrieval floor — a closest-candidate, NOT a start.
    Crediting it as retrieval inflates MRR with a call the router refused to serve.

    The result-level ``no_start`` flag cannot catch this: a result can serve a
    genuine start for one program AND carry a demoted guess for the gold's.
    """
    result = _result(
        _point("start", "meteora", "swap"),
        _point("guess", "pumpfun", "buy"),
    )
    row = GoldenRow("purchase that memecoin", "pumpfun", "buy")
    assert result.no_start is False  # the flag the old code checked says "served"
    assert _gold_rank(result, row) is None


def test_gold_rank_credits_a_surface_card_as_a_genuine_served_point() -> None:
    """``surface`` is a hedge ("start from this program's derive tools"), not a
    below-floor guess — and a golden row with ``gold_instruction is None`` names
    exactly that card as gold. Skipping it would freeze a false negative."""
    result = _result(
        _point("start", "meteora", "swap"),
        _point("surface", "ore", None),
    )
    row = GoldenRow("stake my tokens on ore", "ore", None)
    assert _gold_rank(result, row) == 2


def test_gold_rank_keeps_the_1_based_index_over_the_served_list() -> None:
    """Skipping a guess must not re-index: rank is the position the caller sees."""
    result = _result(
        _point("guess", "metadao_ico", "fund"),
        _point("start", "pumpfun", "buy"),
    )
    row = GoldenRow("buy the token", "pumpfun", "buy")
    assert _gold_rank(result, row) == 2


def test_gold_rank_still_returns_none_when_nothing_cleared_the_floor() -> None:
    result = _result(_point("guess", "pumpfun", "buy"), no_start=True)
    row = GoldenRow("snipe a fresh pump launch", "pumpfun", "buy")
    assert _gold_rank(result, row) is None


def test_gold_rank_only_ever_removes_credit_over_the_golden_set() -> None:
    """C3/C4: the fix is instrumentation, not recovery. Every credited rank is
    still the raw positional index of the gold in ``result.starts`` (no
    re-indexing, no newly-credited row) — the change is removal only."""
    report = evaluate_golden()
    for outcome in report.rows:
        served = outcome.record.top_candidates
        gold_name = candidate_name(
            outcome.row.gold_program or "", outcome.row.gold_instruction
        )
        naive = next(
            (
                rank
                for rank, candidate in enumerate(served, 1)
                if candidate.name == gold_name
            ),
            None,
        )
        if outcome.gold_rank is not None:
            # credited ⇒ same index the naive scan found, and not a guess
            assert outcome.gold_rank == naive
            assert served[outcome.gold_rank - 1].kind != "guess"


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


def test_the_bonding_paraphrase_is_a_refusal_not_a_rank_4_retrieval() -> None:
    """The gold pumpfun/buy IS served for this paraphrase — but demoted to a
    ``guess``, i.e. the router explicitly declined to call it a start. It stays a
    recorded misrank (genuine paraphrase evidence); what it must NOT do is
    contribute 1/4 of a hit to MRR for a call we refused to serve."""
    by_intent = {o.row.intent: o for o in evaluate_golden().rows}
    row = by_intent["purchase some of that new memecoin before it bonds"]
    served = {c.name: c.kind for c in row.record.top_candidates}
    assert served.get("pumpfun/buy") == "guess"
    assert row.gold_rank is None
    assert row.cause == "misrank"  # the cause vocabulary is untouched


def test_surface_gold_rows_keep_their_ranks() -> None:
    """The `surface`-as-gold rows (``gold_instruction is None``) must keep
    counting — they are genuine served points, not below-floor guesses."""
    by_intent = {o.row.intent: o for o in evaluate_golden().rows}
    assert by_intent["stake my tokens on ore"].gold_rank == 1
    assert by_intent["mine ore and claim the rewards"].gold_rank == 2
    assert by_intent["refund my contribution from a failed ico"].gold_rank == 1


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
