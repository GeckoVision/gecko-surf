"""Score the any-query planner against the golden set. $0 by default.

Recorded (default): replays the replies in ``tests/fixtures/anyquery/planner_replies.json``
through the real parse -> validate -> quote path. Those replies are hand-written stand-ins
until a live run replaces them, so the recorded score measures the pipeline, not a model.

Live (``--live``): asks the Claude API (key from the OS keychain: ``gecko auth set
anthropic``) once per golden query. Costs real money; the coordinator runs it, and
``--record PATH`` saves the replies so every later run can replay them at $0.

    uv run python scripts/anyquery_planner_eval.py
    uv run python scripts/anyquery_planner_eval.py --details-dir path/to/orthogonal-details --ledger ~/.gecko/orthogonal/field-ledger.jsonl
    uv run python scripts/anyquery_planner_eval.py --live --record /tmp/replies.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gecko.anyquery.catalog_load import load_catalog
from gecko.anyquery.config import AnyQueryConfig
from gecko.anyquery.golden import GoldenQuery, load_golden, retrieval_recall, run_golden
from gecko.anyquery.model_planner import (
    DEFAULT_PLANNER_MODEL,
    ModelCall,
    ModelPlanner,
    anthropic_call,
)
from gecko.anyquery.planner import PlanRejectedError
from gecko.anyquery.retrieve import bm25_candidates

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "anyquery"
NOW = "2026-10-08T12:00:00Z"


def _replay(replies: dict[str, str], qid: str) -> ModelCall:
    def call(system: str, user: str) -> str:
        if qid not in replies:
            raise PlanRejectedError("no recorded reply for this query")
        return replies[qid]

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--details-dir", type=Path, default=None)
    parser.add_argument("--ledger", type=Path, action="append", default=[])
    parser.add_argument("--golden", type=Path, default=FIXTURES / "golden_queries.json")
    parser.add_argument(
        "--replies", type=Path, default=FIXTURES / "planner_replies.json"
    )
    parser.add_argument("--k", type=int, default=40)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default=DEFAULT_PLANNER_MODEL)
    parser.add_argument("--record", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.details_dir is not None:
        loaded = load_catalog(args.details_dir, ledgers=args.ledger)
        source = str(args.details_dir)
    else:
        subset = json.loads((FIXTURES / "orthogonal_details_subset.json").read_text())
        ledgers = args.ledger or [FIXTURES / "observed_fields.jsonl"]
        loaded = load_catalog(subset["records"], ledgers=ledgers)
        source = "committed subset"
    queries, refusals = load_golden(args.golden)
    print(
        f"catalog: {source}: {len(loaded.candidates.candidates)} candidates; "
        f"kept out: {loaded.report.dropped}"
    )

    for k in sorted({20, args.k}):
        hits, total, missed = retrieval_recall(
            queries, loaded.candidates, bm25_candidates, k
        )
        print(f"retrieval recall@{k}: {hits}/{total}  missed: {missed or '-'}")

    recorded: dict[str, str] = {}
    if args.live:
        live = anthropic_call(args.model)

        def planner_for(item: GoldenQuery) -> ModelPlanner:
            def call(system: str, user: str) -> str:
                reply = live(system, user)
                recorded[item.qid] = reply
                return reply

            return ModelPlanner(call=call, model=args.model)
    else:
        replies = json.loads(args.replies.read_text())["replies"]

        def planner_for(item: GoldenQuery) -> ModelPlanner:
            return ModelPlanner(call=_replay(replies, item.qid), model="recorded")

    run = run_golden(
        queries,
        refusals,
        catalog=loaded.candidates,
        planner_for=planner_for,
        config=AnyQueryConfig(),
        now=NOW,
        k=args.k,
        confirmed=loaded.confirmed,
    )
    for o in run.outcomes:
        print(f"  {o.qid:20} {o.field:18} {o.outcome:17} {o.picked or ''}")
    for qid, why in run.refused.items():
        print(f"  refused {qid}: {why}")
    for qid, micro in run.quotes.items():
        print(f"  quote {qid}: {micro / 1_000_000:.4f} USDC")
    print(f"first-source-correct: {run.correct}/{len(run.outcomes)}")
    print(
        f"refusals held: {len(run.refusals_held)}/{len(refusals)}"
        + (f"  BROKEN: {list(run.refusals_broken)}" if run.refusals_broken else "")
    )
    if args.record is not None and recorded:
        args.record.write_text(
            json.dumps(
                {
                    "about": f"live replies from {args.model}, {NOW[:10]} eval run",
                    "replies": recorded,
                },
                indent=1,
            )
        )
        print(f"recorded {len(recorded)} replies to {args.record}")
    return 0 if run.correct == len(run.outcomes) and not run.refusals_broken else 1


if __name__ == "__main__":
    sys.exit(main())
