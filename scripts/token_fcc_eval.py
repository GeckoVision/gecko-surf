"""The advantage-proof: Gecko's projected surface uses far fewer tokens AND stays
first-call-correct — vs the naive "dump every operation as a tool" a DIY builder ships.

Two numbers, one run, per fixture:
  * TOKEN COST (deterministic, tiktoken cl100k) of the tool defs the model sees —
    RAW arm (every op dumped) vs GECKO arm (search-surfaced top-k).
  * FCC (first-call-correct) for each arm — the guarantee that the compression did
    NOT break tool selection (run with Haiku; needs CLAUDE_API_KEY).

Headline: "-N% tokens at equal-or-better first-call-correct." Compression is the
ADVANTAGE of using Gecko; staying first-call-correct is what makes it ownable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import tiktoken

from gecko.client import AgentApiClient
from gecko.evaluate import load_golden
from gecko.fcc_eval import (
    evaluate_fcc,
    fcc_rate,
    gecko_tools,
    raw_tools,
)

ENC = tiktoken.get_encoding("cl100k_base")
K = 8
ROOT = Path(__file__).resolve().parents[1]

FIXTURES = [
    (
        "TxLINE",
        ROOT / "tests/fixtures/txodds_docs.yaml",
        ROOT / "tests/fixtures/golden/txodds_tasks.jsonl",
    ),
    (
        "Pegana",
        ROOT / "examples/pegana_demo/spec/pegana_openapi.json",
        ROOT / "tests/fixtures/golden/pegana_tasks.jsonl",
    ),
]


def _tokens(defs: list[dict[str, Any]]) -> int:
    """Token cost of the tool defs the model is shown (Anthropic tool schema)."""
    return len(ENC.encode(json.dumps(defs, separators=(",", ":"))))


def main() -> None:
    key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    llm = None
    if key:
        import sys

        sys.path.insert(0, str(ROOT / "examples"))
        from sos_vzla_bot.providers import make_llm  # type: ignore

        llm = make_llm("anthropic", key)

    print(
        f"{'fixture':10} {'RAW tok':>8} {'GECKO tok':>10} {'reduce':>7}   "
        f"{'RAW fcc':>8} {'GECKO fcc':>10}"
    )
    print("-" * 62)
    for name, spec, tasks_path in FIXTURES:
        client = AgentApiClient(str(spec))
        tasks = load_golden(tasks_path)

        # RAW: every op dumped once (constant per fixture).
        raw_tok = _tokens([t.anthropic() for t in raw_tools(client.operations)])
        # GECKO: per-goal top-k, averaged over the task set.
        gk_toks = [
            _tokens([t.anthropic() for t in gecko_tools(client, t_.goal, K)])
            for t_ in tasks
        ]
        gk_tok = round(sum(gk_toks) / len(gk_toks)) if gk_toks else 0
        reduce_pct = (1 - gk_tok / raw_tok) * 100 if raw_tok else 0.0

        raw_fcc = gk_fcc = None
        if llm is not None:
            records = evaluate_fcc(
                name, client, tasks, llm, model="claude-haiku-4-5", k=K, n_runs=3
            )
            raw_fcc = fcc_rate(records, "raw")
            gk_fcc = fcc_rate(records, "gecko")

        rf = f"{raw_fcc:.2f}" if raw_fcc is not None else "  n/a"
        gf = f"{gk_fcc:.2f}" if gk_fcc is not None else "  n/a"
        print(
            f"{name:10} {raw_tok:>8} {gk_tok:>10} {reduce_pct:>6.1f}%   "
            f"{rf:>8} {gf:>10}"
        )

    print("\nToken cost = the tool defs the model sees per turn (tiktoken cl100k).")
    print("FCC = first-call-correct rate over the golden tasks (Haiku, n_runs=3).")


if __name__ == "__main__":
    main()
