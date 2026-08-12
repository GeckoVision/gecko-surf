"""Retrieval-gate instrumentation v2 — the eval runner for :mod:`gecko.find_start`.

The design constraint on find_start is "lexical, no vectors; the semantic gate
stays closed and opens only on evidence". But the production ``on_miss`` seam
fires ONLY at score 0 — misranks (the gold start IS wired but ranks below top-k),
the only real evidence FOR a semantic retriever, never log. This module closes
that loop: a small committed golden set (realistic intents + their gold starts)
is replayed through the UNMODIFIED router, and every row is classified into the
CLOSED miss-cause vocabulary declared in :mod:`gecko.find_start`.

Reading the report (the framing is part of the contract):

- ``vocabulary_gap`` + ``misrank`` are the ONLY causes that count as evidence
  FOR the semantic flip — the gold start is wired, lexical scoring just fails it.
- ``coverage_gap`` argues for wiring more programs/intents, NOT for vectors.
- ``false_accept`` (an out-of-scope intent clearing the floor) is precision/floor
  evidence, also not a ranking argument.

This module MEASURES the case; it never flips anything. Nothing here persists
intent text — the golden set is committed, authored, public data, and the
records it produces carry names/scores/ranks only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Mapping, Sequence

from .find_start import (
    CandidateRecord,
    FindStartResult,
    FloorAdmission,
    FloorKind,
    MissCause,
    MissRecord,
    _card_terms,
    _Card,
    _distinguishing_terms,
    _query_tokens,
    _wired_cards,
    candidate_name,
    find_start,
    floor_admission,
)

__all__ = [
    "BranchCount",
    "GoldenRow",
    "GoldenSetError",
    "RetrievalReport",
    "RowOutcome",
    "classify_miss",
    "default_golden_text",
    "evaluate_golden",
    "false_accepts_admitted_by_corroboration",
    "false_accepts_admitted_by_name",
    "format_report",
    "load_golden",
]

# The committed default golden set, packaged so the CLI default works installed.
_GOLDEN_RESOURCE = ("orquestra", "find_start_golden.jsonl")

# How deep the router is searched during eval — deeper than the CLI default so a
# gold start ranked anywhere among the wired cards gets a real rank, and a
# misrank (rank > k) is distinguishable from "absent entirely".
EVAL_LIMIT = 10

# Rows above the range hint that the "small committed fixture" is drifting into a
# corpus; keep the golden set reviewable.
MAX_GOLDEN_ROWS = 200


class GoldenSetError(Exception):
    """The golden set failed to parse/validate (it is committed data — fail loud)."""


@dataclass(frozen=True)
class GoldenRow:
    """One authored eval row. ``gold_program is None`` marks a deliberately
    out-of-scope intent (the floor must reject it); ``gold_instruction is None``
    with a program marks the program's surface card as gold."""

    intent: str
    gold_program: str | None
    gold_instruction: str | None
    note: str = ""


@dataclass(frozen=True)
class RowOutcome:
    """One evaluated row: the golden row, its rank facts, its closed-vocabulary
    cause, and the extended categorical :class:`MissRecord` (eval-only fields
    populated — the same record shape production emits, minus intent text)."""

    row: GoldenRow
    cause: MissCause
    gold_rank: int | None
    top1_name: str | None
    top1_score: int
    margin: int
    floor: FloorKind
    record: MissRecord
    #: Which floor branch admitted each RUNNABLE start served for this row, computed by
    #: :func:`gecko.find_start.floor_admission` — the same function the router gates
    #: with, never a second copy. Empty when nothing was served as a start.
    admissions: tuple[FloorAdmission, ...] = ()


@dataclass(frozen=True)
class RetrievalReport:
    """Aggregate retrieval metrics over the golden set.

    ``scoreable`` counts rows whose gold IS wired — the recall/MRR denominator.
    Coverage gaps and out-of-scope rows are reported separately: including
    un-retrievable golds in recall would conflate wiring with ranking."""

    rows: tuple[RowOutcome, ...]
    k: int
    scoreable: int
    recall_at_1: float
    recall_at_3: float
    mrr: float
    causes: Mapping[str, int]
    semantic_evidence: int  # misrank + vocabulary_gap — the only flip evidence
    out_of_scope: int
    false_accepts: int


def default_golden_text() -> str:
    """The committed golden set's raw JSONL (packaged data)."""
    anchor = resources.files("gecko.providers.configs").joinpath(*_GOLDEN_RESOURCE)
    return anchor.read_text(encoding="utf-8")


def load_golden(text: str) -> tuple[GoldenRow, ...]:
    """Parse + validate golden JSONL. Committed data, but validated anyway —
    a malformed row must fail loud, not skew the metrics silently."""
    rows: list[GoldenRow] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise GoldenSetError(f"golden line {lineno}: invalid JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise GoldenSetError(f"golden line {lineno}: expected an object")
        intent = raw.get("intent")
        program = raw.get("gold_program")
        instruction = raw.get("gold_instruction")
        note = raw.get("note", "")
        if not isinstance(intent, str) or not intent.strip():
            raise GoldenSetError(f"golden line {lineno}: 'intent' must be a string")
        if program is not None and not isinstance(program, str):
            raise GoldenSetError(
                f"golden line {lineno}: 'gold_program' must be a string or null"
            )
        if instruction is not None and not isinstance(instruction, str):
            raise GoldenSetError(
                f"golden line {lineno}: 'gold_instruction' must be a string or null"
            )
        if program is None and instruction is not None:
            raise GoldenSetError(
                f"golden line {lineno}: gold_instruction without gold_program"
            )
        if not isinstance(note, str):
            raise GoldenSetError(f"golden line {lineno}: 'note' must be a string")
        rows.append(GoldenRow(intent, program, instruction, note))
    if not rows:
        raise GoldenSetError("golden set is empty")
    if len(rows) > MAX_GOLDEN_ROWS:
        raise GoldenSetError(
            f"golden set has {len(rows)} rows (cap {MAX_GOLDEN_ROWS}) — "
            "keep the committed fixture small and reviewable"
        )
    return tuple(rows)


def classify_miss(
    *,
    has_content_tokens: bool,
    out_of_scope: bool,
    gold_wired: bool,
    no_start: bool,
    gold_rank: int | None,
    gold_overlap: int,
    k: int,
) -> MissCause:
    """Map one row's facts onto the CLOSED miss-cause vocabulary (pure — the
    branch table under test). Order matters: token emptiness first, scope second,
    wiring third; only then is ranking judged."""
    if not has_content_tokens:
        return "no_content_tokens"
    if out_of_scope:
        # gold=null rows measure the FLOOR: an honest rejection is the hit.
        return "hit" if no_start else "false_accept"
    if not gold_wired:
        return "coverage_gap"
    if gold_rank is not None and gold_rank <= k:
        return "hit"
    if gold_overlap == 0:
        return "vocabulary_gap"
    return "misrank"


def _gold_rank(result: FindStartResult, row: GoldenRow) -> int | None:
    """1-based rank of the gold start among GENUINE served points (guesses are
    below the floor — they never count as retrieval)."""
    if result.no_start:
        return None
    for rank, point in enumerate(result.starts, 1):
        if point.program == row.gold_program and point.instruction == (
            row.gold_instruction
        ):
            return rank
    return None


def _admissions(
    result: FindStartResult, cards: Sequence[_Card], q_tokens: set[str]
) -> tuple[FloorAdmission, ...]:
    """The floor branch that admitted each RUNNABLE start this row was served.

    Recomputed with :func:`gecko.find_start.floor_admission` — the router's own gate,
    on the router's own inputs — rather than re-derived here, so this can never report
    a branch the router did not actually take."""
    if result.no_start:
        return ()
    by_name = {(c.api_id, c.instruction): c for c in cards}
    distinguishing = _distinguishing_terms(q_tokens, cards)
    out: list[FloorAdmission] = []
    for point in result.starts:
        if point.kind != "start":
            continue
        card = by_name.get((point.program, point.instruction))
        if card is None:
            continue
        out.append(floor_admission(q_tokens & _card_terms(card), card, distinguishing))
    return tuple(out)


def _evaluate_row(
    row: GoldenRow,
    cards_by_gold: Mapping[tuple[str, str | None], _Card],
    cards: Sequence[_Card],
    wired_program_count: int,
    k: int,
) -> RowOutcome:
    result = find_start(row.intent, limit=EVAL_LIMIT)
    q_tokens = _query_tokens(row.intent)
    gold_key = (row.gold_program or "", row.gold_instruction)
    gold_card = cards_by_gold.get(gold_key) if row.gold_program else None
    gold_overlap = len(q_tokens & _card_terms(gold_card)) if gold_card else 0
    rank = _gold_rank(result, row)
    cause = classify_miss(
        has_content_tokens=bool(q_tokens),
        out_of_scope=row.gold_program is None,
        gold_wired=gold_card is not None,
        no_start=result.no_start,
        gold_rank=rank,
        gold_overlap=gold_overlap,
        k=k,
    )
    candidates = tuple(
        CandidateRecord(
            name=candidate_name(p.program, p.instruction), score=p.score, kind=p.kind
        )
        for p in result.starts
    )
    top1 = candidates[0] if candidates else None
    margin = candidates[0].score - candidates[1].score if len(candidates) > 1 else 0
    floor: FloorKind = "guess" if result.no_start else "start"
    record = MissRecord(
        intent_term_count=len(q_tokens),
        matched_score=top1.score if top1 else 0,
        wired_program_count=wired_program_count,
        top_candidates=candidates,
        margin=margin,
        floor=floor,
        gold_rank=rank,
        miss_cause=cause,
    )
    return RowOutcome(
        row=row,
        cause=cause,
        gold_rank=rank,
        top1_name=top1.name if top1 else None,
        top1_score=top1.score if top1 else 0,
        margin=margin,
        floor=floor,
        record=record,
        admissions=_admissions(result, cards, q_tokens),
    )


def evaluate_golden(
    golden_path: str | Path | None = None, *, k: int = 3
) -> RetrievalReport:
    """Replay the golden set through the unmodified router and aggregate.

    ``golden_path`` overrides the committed default fixture. ``k`` is the
    classification threshold: gold ranked within top-k is a hit; wired-with-
    overlap below it is a misrank."""
    text = (
        Path(golden_path).read_text(encoding="utf-8")
        if golden_path is not None
        else default_golden_text()
    )
    rows = load_golden(text)

    cards = _wired_cards()
    cards_by_gold = {(c.api_id, c.instruction): c for c in cards}
    wired_program_count = len({c.api_id for c in cards})

    outcomes = tuple(
        _evaluate_row(row, cards_by_gold, cards, wired_program_count, k) for row in rows
    )

    causes: dict[str, int] = {}
    for outcome in outcomes:
        causes[outcome.cause] = causes.get(outcome.cause, 0) + 1

    scoreable = [
        o
        for o in outcomes
        if o.row.gold_program is not None
        and (o.row.gold_program, o.row.gold_instruction) in cards_by_gold
    ]
    n = len(scoreable)
    recall_at_1 = sum(1 for o in scoreable if o.gold_rank == 1) / n if n else 0.0
    recall_at_3 = (
        sum(1 for o in scoreable if o.gold_rank is not None and o.gold_rank <= 3) / n
        if n
        else 0.0
    )
    mrr = (
        sum(1 / o.gold_rank for o in scoreable if o.gold_rank is not None) / n
        if n
        else 0.0
    )

    return RetrievalReport(
        rows=outcomes,
        k=k,
        scoreable=n,
        recall_at_1=recall_at_1,
        recall_at_3=recall_at_3,
        mrr=mrr,
        causes=causes,
        semantic_evidence=causes.get("misrank", 0) + causes.get("vocabulary_gap", 0),
        out_of_scope=sum(1 for o in outcomes if o.row.gold_program is None),
        false_accepts=causes.get("false_accept", 0),
    )


# --- the false-accept split (one function per floor branch) ----------------------


@dataclass(frozen=True)
class BranchCount:
    """A count that CANNOT be quoted without its denominator.

    A bare "false_accepts: 8" says nothing about whether 8 is out of 8 or out of 80,
    and the out-of-scope block is small enough that the difference is the whole
    finding. ``__str__`` is the only rendering, and it always prints both.
    """

    branch: str
    count: int
    denominator: int

    def __str__(self) -> str:
        return f"{self.branch}={self.count}/{self.denominator}"


def _false_accept_rows(report: RetrievalReport) -> tuple[RowOutcome, ...]:
    return tuple(o for o in report.rows if o.cause == "false_accept")


def false_accepts_admitted_by_name(report: RetrievalReport) -> BranchCount:
    """Out-of-scope rows admitted because a query term NAMED a program/instruction.

    Denominator: every out-of-scope row. This is the branch that needs no
    corroboration at all — one matched identity term is sufficient, on its own, for a
    RUNNABLE start — so it is the branch a retrieval change is most likely to move by
    accident, and the one that must be reported separately."""
    rows = [o for o in _false_accept_rows(report) if _touched(o, ("named", "both"))]
    return BranchCount("named", len(rows), report.out_of_scope)


def false_accepts_admitted_by_corroboration(report: RetrievalReport) -> BranchCount:
    """Out-of-scope rows admitted because two DISTINGUISHING terms agreed.

    Denominator: every out-of-scope row. Rows where a served start was admitted on
    BOTH branches are counted here AND in :func:`false_accepts_admitted_by_name` — the
    two figures answer "did this branch admit it", not "which one gets the blame", so
    they are not required to sum to the total."""
    rows = [
        o for o in _false_accept_rows(report) if _touched(o, ("corroborated", "both"))
    ]
    return BranchCount("corroborated", len(rows), report.out_of_scope)


def _touched(outcome: RowOutcome, branches: tuple[str, ...]) -> bool:
    return any(a in branches for a in outcome.admissions)


# --- legible rendering (the CLI prints this verbatim) ----------------------------

_CAUSE_ORDER = (
    "hit",
    "misrank",
    "vocabulary_gap",
    "coverage_gap",
    "false_accept",
    "no_content_tokens",
)


def format_report(report: RetrievalReport) -> str:
    lines: list[str] = []
    lines.append(f"retrieval eval — {len(report.rows)} golden rows, k={report.k}")
    lines.append("")
    header = f"{'cause':<17} {'rank':>4} {'top1(score)':<28} {'margin':>6}  intent"
    lines.append(header)
    lines.append("-" * len(header))
    for outcome in report.rows:
        rank = str(outcome.gold_rank) if outcome.gold_rank is not None else "-"
        top1 = (
            f"{outcome.top1_name}({outcome.top1_score})"
            if outcome.top1_name
            else "(none)"
        )
        if outcome.floor == "guess":
            top1 += " GUESS" if outcome.top1_name else ""
        lines.append(
            f"{outcome.cause:<17} {rank:>4} {top1:<28} {outcome.margin:>6}  "
            f"{outcome.row.intent}"
        )
    lines.append("")
    lines.append(
        f"recall@1 {report.recall_at_1:.2f} | recall@3 {report.recall_at_3:.2f} | "
        f"MRR {report.mrr:.2f}  (over {report.scoreable} wired-gold rows)"
    )
    histogram = ", ".join(
        f"{cause}={report.causes[cause]}"
        for cause in _CAUSE_ORDER
        if cause in report.causes
    )
    lines.append(f"causes: {histogram}")
    lines.append("")
    lines.append(
        "semantic-flip evidence: "
        f"{report.causes.get('misrank', 0)} misrank + "
        f"{report.causes.get('vocabulary_gap', 0)} vocabulary_gap "
        f"of {report.scoreable} wired-gold rows "
        "(threshold: documented here, not auto-flipping anything — the gate opens "
        "on sustained misrank/vocabulary_gap dominance over realistic paraphrases, "
        "a reviewed decision, never a code path)"
    )
    lines.append(
        f"coverage gaps ({report.causes.get('coverage_gap', 0)}) argue for wiring "
        "more programs/intents, NOT for a semantic retriever."
    )
    if report.out_of_scope:
        lines.append(
            f"floor check: {report.out_of_scope - report.false_accepts}/"
            f"{report.out_of_scope} out-of-scope intents honestly rejected; "
            f"{report.false_accepts} false accept(s) — precision evidence about "
            "the floor, not the ranker."
        )
        by_name = false_accepts_admitted_by_name(report)
        by_corroboration = false_accepts_admitted_by_corroboration(report)
        lines.append(
            f"  admitted by branch: {by_name} | {by_corroboration} "
            "(a row admitted on both branches is counted in both; the split, not the "
            "total, is what says which branch a change moved)"
        )
    return "\n".join(lines) + "\n"
