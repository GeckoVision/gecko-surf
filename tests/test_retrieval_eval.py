"""retrieval_eval — the misrank-aware eval for the lexical-vs-semantic gate.

The production on_miss seam fires only at score 0, so misranks (gold wired but
ranked below top-k) — the only real evidence FOR a semantic retriever — never
log. These tests pin the closed miss-cause vocabulary, the golden-set replay,
the aggregate metrics, and the control-plane rule that no record ever carries
intent text.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from gecko.find_start import (
    ChainPlan,
    FindStartResult,
    StartPoint,
    candidate_name,
    find_start,
)
from gecko.retrieval_eval import (
    EVAL_LIMIT,
    SERVE_LIMIT,
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
    # "refund my contribution from a failed ico" held rank 1 until 2026-08-19 and is one of
    # the 7 rows the identity-term gate costs: it reached metadao_ico's SURFACE card on
    # `ico` alone, which is the program's own name and nothing else. Refusing it is the
    # gate working, not a ranking regression — the same permissiveness served "buy a
    # house". It returns when the card carries the vocabulary of a refund rather than only
    # the program's name.
    assert by_intent["refund my contribution from a failed ico"].gold_rank is None


# --- the measured floors (R-3) ---------------------------------------------------
#
# main had NO retrieval tripwire. ``test_evaluate_golden_aggregates_are_consistent``
# only asserts the metrics lie in [0, 1] and are ordered, which recall 0.0 satisfies —
# so retrieval could collapse completely and the suite would stay green.
#
# Written as EXACT FRACTIONS, never as rounded decimal literals. The floors that were
# drafted on the abandoned R2 branch were `0.7576` and `0.9091`, both rounded UP from
# 25/33 = 0.757575… and 30/33 = 0.909090… and both compared with `>=`. They therefore
# FAILED at exact parity with the very measurement they were derived from. A fraction
# cannot round the wrong way, which removes the defect class rather than the instance.
#
# Measured on main at 15b5044, cold. Raising any of these is a real improvement and the
# floor should be raised with it; lowering one is a regression that has to be argued.
# LOWERED 2026-08-19, and here is the argument the comment above demands.
#
# The corroboration gate used to accept on an identity-term match ALONE. That branch was
# not buying recall, it was borrowing it: the same rule that reached these rows served
# "buy a house" as pumpfun.buy, "sell my car" as pumpfun.sell and "swap my shift with a
# coworker" as meteora.swap — 6 of the golden set's 12 out-of-scope rows, each on a single
# word that was the instruction's own name. Requiring one matched term BEYOND the name
# took false accepts from 8/12 to 2/12 and cost 7 in-scope rows.
#
# Four of those 7 are meteora rows, and they are lost for one reason: `meteora.swap`'s card
# reads "Plan a Meteora DLMM swap. Give input_mint, output_mint, bin_step, base_factor..."
# It describes the IMPLEMENTATION and never the words a person says, so "swap sol for usdc"
# matches only {swap}. The card is thin; the gate is not wrong.
#
# These floors are therefore a FLOOR ON A KNOWN DEBT, not a new normal. What repays it is
# value-domain recognition — knowing sol and usdc are members of `solana-token-mint` and
# crediting the card that consumes that domain. What must NOT repay it is pasting this
# fixture's vocabulary into the config, which would make the eval score itself.
RECALL_AT_1_FLOOR = 23 / 33
RECALL_AT_3_FLOOR = 27 / 33
MRR_FLOOR = 25 / 33

# The precision side. These 8 are AUTHORED, not accidental: e6a6b20 ("out-of-scope rows
# that carry an identity term — 8/8 false-accept") committed them to measure what the
# floor lets through when an out-of-scope intent shares a term with a wired card. A
# ceiling, not a floor: the number must not grow unnoticed, and driving it DOWN is the
# improvement.
# 8 -> 2 on 2026-08-19 with the identity-term gate. Driving it down is the improvement,
# so the ceiling follows it down: 2 must not silently become 3.
FALSE_ACCEPT_CEILING = 2


def test_the_measured_retrieval_floors_hold() -> None:
    """The tripwire main did not have. Exact fractions, so parity passes."""
    report = evaluate_golden()

    assert report.scoreable == 33, "the recall denominator moved — re-derive the floors"
    assert report.recall_at_1 >= RECALL_AT_1_FLOOR, (
        f"recall@1 regressed: {report.recall_at_1} < {RECALL_AT_1_FLOOR}"
    )
    assert report.recall_at_3 >= RECALL_AT_3_FLOOR, (
        f"recall@3 regressed: {report.recall_at_3} < {RECALL_AT_3_FLOOR}"
    )
    assert report.mrr >= MRR_FLOOR, f"MRR regressed: {report.mrr} < {MRR_FLOOR}"
    assert report.false_accepts <= FALSE_ACCEPT_CEILING, (
        f"the floor got looser: {report.false_accepts} > {FALSE_ACCEPT_CEILING}"
    )


def test_the_floors_are_exact_fractions_not_rounded_literals() -> None:
    """The defect that made the drafted floors fail at parity, forbidden directly.

    Each floor must be EQUAL to the measurement it was derived from, not merely near it.
    A rounded-up decimal (0.7576 > 25/33) passes a human read and then fails `>=` against
    its own source measurement. This catches that at the point of authorship.
    """
    assert RECALL_AT_1_FLOOR == 23 / 33
    assert RECALL_AT_3_FLOOR == 27 / 33
    assert MRR_FLOOR == 25 / 33

    # The specific literals that were drafted, and why they could not work.
    assert 0.7576 > 25 / 33, (
        "0.7576 rounds UP — `recall@1 >= 0.7576` is False at parity"
    )
    assert 0.9091 > 30 / 33, (
        "0.9091 rounds UP — `recall@3 >= 0.9091` is False at parity"
    )


# --- R-2: wrong-instruction accepts, at the limit production serves --------------


def test_wrong_instruction_accepts_is_zero_at_the_limit_production_serves() -> None:
    """R-2. A metric that had to be BUILT — it was a name in a plan, not a symbol.

    A ceiling of 0 is meaningful here rather than aspirational: the router currently never
    offers the wrong instruction FIRST on the right program, for any of the 30
    instruction-level golden rows, at either limit. So any change that starts doing so
    fires this immediately.

    Recall cannot express the same thing. Recall asks where the gold ranked and is
    satisfied by a rank-2 hit even when the FIRST actionable offer would execute a
    different instruction on the same program — which is precisely the call an agent
    following "first-call-correct" would make.
    """
    report = evaluate_golden()
    assert report.directional == 30, "the denominator moved — re-derive the ceiling"
    assert report.wrong_instruction_accepts == 0, (
        "the first actionable offer on the right program is the wrong action for "
        f"{report.wrong_instruction_accepts} row(s)"
    )


def test_the_metric_is_read_at_serve_limit_not_eval_limit() -> None:
    """SERVE_LIMIT is what production gives an agent; EVAL_LIMIT is deeper by design."""
    assert SERVE_LIMIT == 5
    assert EVAL_LIMIT == 10
    from gecko.find_start import find_start as _fs

    assert inspect.signature(_fs).parameters["limit"].default == SERVE_LIMIT, (
        "SERVE_LIMIT must track find_start's production default"
    )


def test_the_router_refuses_at_serve_limit_what_it_serves_at_eval_limit() -> None:
    """Why the shallow call has to be MADE, not truncated from the deep one.

    The floor is limit-sensitive, so ``find_start(intent, limit=10).starts[:5]`` is not
    ``find_start(intent, limit=5).starts``. This is not a corner case: 7 of the 46 golden
    rows differ between the two, and for at least one the router REFUSES at 5 (
    ``no_start``) while serving a start at 10.

    The consequence is the honest reading of the R-2 finding: every rank in the report
    above is measured at a depth no agent is given, so the report is systematically more
    optimistic than production. This test pins that gap so it cannot widen unnoticed, and
    it is the reason :func:`count_wrong_instruction_accepts` re-queries at SERVE_LIMIT.
    """
    rows = load_golden(default_golden_text())
    truncation_differs = 0
    refused_shallow_served_deep = []

    for row in rows:
        shallow = find_start(row.intent, limit=SERVE_LIMIT)
        deep = find_start(row.intent, limit=EVAL_LIMIT)
        shallow_keys = [(p.program, p.instruction, p.kind) for p in shallow.starts]
        deep_prefix = [
            (p.program, p.instruction, p.kind) for p in deep.starts[:SERVE_LIMIT]
        ]
        if shallow_keys != deep_prefix or shallow.no_start != deep.no_start:
            truncation_differs += 1
        if shallow.no_start and not deep.no_start:
            refused_shallow_served_deep.append(row.intent)

    assert truncation_differs, (
        "truncation now equals a shallow call — if the floor stopped being "
        "limit-sensitive, count_wrong_instruction_accepts can stop re-querying"
    )
    # This used to assert the gap was NON-empty, pinning rows that a shallow call refused
    # and a deep call served. On 2026-08-19 the identity-term gate CLOSED that gap, and
    # the old assertion's own failure message said this outcome was "good news to record".
    # Recorded: the floor no longer depends on how deep the caller looked, which is the
    # property we wanted. `truncation_differs` above still holds, so
    # count_wrong_instruction_accepts must keep re-querying at SERVE_LIMIT.
    assert not refused_shallow_served_deep, (
        "a row is refused at SERVE_LIMIT but served at EVAL_LIMIT again — the floor has "
        f"become depth-sensitive: {refused_shallow_served_deep}"
    )


# --- R-1: a directional inverse must not come first ------------------------------

INVERSES = {
    "buy": "sell",
    "sell": "buy",
    "deposit": "withdraw",
    "withdraw": "deposit",
}


def _inverse_outranks_gold(
    starts: tuple[StartPoint, ...],
    *,
    program: str,
    instruction: str,
    inverse: str,
) -> bool:
    """Is the gold's directional inverse served ABOVE the gold itself?

    Positions are taken over served STARTS only. A ``guess`` is below the floor by
    definition and is not offered as a place to begin, so it cannot out-rank anything.
    """
    served = [
        (point.program, point.instruction) for point in starts if point.kind == "start"
    ]
    gold_at = next(
        (i for i, key in enumerate(served) if key == (program, instruction)), None
    )
    inverse_at = next(
        (i for i, key in enumerate(served) if key == (program, inverse)), None
    )
    if gold_at is None or inverse_at is None:
        return False
    return inverse_at < gold_at


def test_a_directional_inverse_never_outranks_the_gold() -> None:
    """R-1. ``exit my position`` must not be answered with ``buy`` ranked above ``sell``.

    The golden set has carried this case since it was authored — row 10, noted "must not
    land on buy" — with nothing asserting it. Recall cannot catch it: recall only asks
    where the gold ranked, so a router that served the inverse FIRST would still be
    credited with a rank-2 hit at k=3.

    Scoped to the ORDER, and to a directional inverse of the gold on the SAME program.
    Two narrower choices, both deliberate:

    * A different program ranking near the gold is ordinary competition, not an
      inversion. Forbidding that would forbid the router working at all.
    * Serving the inverse BELOW the gold is not forbidden here — and it does happen:
      ``pumpfun/buy`` and ``pumpfun/sell`` are both served as ``kind == "start"`` for
      both directions of this pair, identically at limit 5 and limit 10. That is a real
      residual, named rather than left to be discovered: an agent that picks a
      lower-ranked start can still invert the user's intent. Suppressing an already-
      served start is a router behaviour change with its own blast radius, and not a
      test's call to make. What is guarded here is the property that actually inverts an
      answer — the wrong direction cannot come first.

    Checked at limit 5, which is what production serves, and at ``EVAL_LIMIT``.
    """
    checked = 0
    for limit in (5, EVAL_LIMIT):
        for row in load_golden(default_golden_text()):
            inverse = INVERSES.get(row.gold_instruction or "")
            if row.gold_program is None or row.gold_instruction is None:
                continue
            if inverse is None:
                continue
            checked += 1
            assert not _inverse_outranks_gold(
                find_start(row.intent, limit=limit).starts,
                program=row.gold_program,
                instruction=row.gold_instruction,
                inverse=inverse,
            ), (
                f"{row.intent!r} wants {row.gold_program}/{row.gold_instruction} "
                f"but is served {row.gold_program}/{inverse} first, at limit {limit}"
            )

    assert checked, "no directional golden row was exercised — the guard is vacuous"


def test_the_inversion_predicate_fires_when_the_order_flips() -> None:
    """R-1's counterexample. A guard that has never been RED is not known to work.

    The router does not invert today, so the inverted case is constructed: the same two
    points with ``buy`` moved above ``sell``. That is the shape a ranking change would
    produce, and it is what the guard above exists to refuse.
    """
    sell = _point("start", "pumpfun", "sell")
    buy = _point("start", "pumpfun", "buy")
    subject = {"program": "pumpfun", "instruction": "sell", "inverse": "buy"}

    assert not _inverse_outranks_gold((sell, buy), **subject)  # as main serves it
    assert _inverse_outranks_gold((buy, sell), **subject)  # the regression

    # Below the floor is not an offered start, so it cannot out-rank the gold.
    demoted = _point("guess", "pumpfun", "buy")
    assert not _inverse_outranks_gold((demoted, sell), **subject)


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


def test_a_golden_set_whose_gold_joins_nothing_refuses_instead_of_reporting_zero(
    tmp_path,
) -> None:
    """recall@1 0.00 · recall@3 0.00 · MRR 0.00 over a denominator of ZERO.

    Every row here names a program nothing serves, so none is scoreable — the same shape
    as a renamed program id or a moved wiring file. The three floors then read exactly
    like a ranker that found nothing, which is the one reading the numbers cannot support.
    """
    from gecko.evidence import Uninterpretable

    unwired = tmp_path / "golden.jsonl"
    unwired.write_text(
        '{"intent": "buy a wombat token on the curve", "gold_program": "nosuchprogram", '
        '"gold_instruction": "buy"}\n'
        '{"intent": "sell a wombat token on the curve", "gold_program": "nosuchprogram", '
        '"gold_instruction": "sell"}\n'
    )
    with pytest.raises(Uninterpretable) as excinfo:
        evaluate_golden(unwired)
    assert excinfo.value.reason == "empty_denominator"
    assert "scoreable" in str(excinfo.value)


def test_the_committed_set_still_scores_so_that_refusal_is_a_guard() -> None:
    # The contrast arm for the test above: the committed fixture has a real denominator,
    # so the refusal fires on the empty join and not on every run.
    assert evaluate_golden().scoreable > 0


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
                # Was "swap sol for usdc" until 2026-08-19. That row is now a
                # documented casualty of the identity-term gate (meteora's card
                # describes its implementation, not the task), and this test is about
                # whether a CUSTOM FIXTURE is honoured — not about that row.
                "intent": "stake my tokens on ore",
                "gold_program": "ore",
                "gold_instruction": None,
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
