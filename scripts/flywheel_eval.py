"""Flywheel FCC eval — the corpus-lift confirmation (three arms: RAW / GECKO / GECKO+CORPUS).

Where ``scripts/fcc_eval.py`` measures the COMPREHENSION lift (GECKO vs the naive RAW dump),
this measures the CORPUS lift: does re-injecting corrections captured from GECKO's own
first-call failures raise FCC beyond comprehension alone? The loop IS the flywheel:

  1. run once (RAW + GECKO) → collect GECKO failures where the right tool was picked but a
     non-obvious required param was under-supplied;
  2. ``corrections_from_records`` → terse, control-plane-safe correctness notes (metadata → metadata);
  3. run again with all three arms → print RAW / GECKO / GECKO+CORPUS FCC + ``lift`` + ``lift_corpus``.

    uv run --extra fcc python scripts/flywheel_eval.py            # N=3, k=8
    uv run --extra fcc python scripts/flywheel_eval.py --n 5 --k 10

HONESTY (scope): this proves the flywheel MECHANISM — IF corrections are captured they
measurably raise FCC beyond comprehension alone. It does NOT prove production auto-capture:
the feedback path (the agent calls the API directly, so Gecko may not see live outcomes) is
still an unresolved design decision (invariant: never store payloads). The corrections here
are derived from our OWN FCC eval telemetry (metadata-only RunRecords) — a legitimate but
controlled capture source. A thin or zero corpus lift on Haiku is a real finding, not a bug.

Reads CLAUDE_API_KEY from the environment or a repo ``.env`` (never printed; not logged).
The offline test (``tests/test_corrections.py``) is the gate — this runner is the final check.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
import pathlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.access import Session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.corrections import corrections_from_records  # noqa: E402
from gecko.enrich import BLURB_MODEL  # noqa: E402  (== "claude-haiku-4-5")
from gecko.evaluate import load_golden  # noqa: E402
from gecko.fcc_eval import (  # noqa: E402
    evaluate_fcc,
    fcc_rate,
    hallucination_rate,
    lift,
    lift_corpus,
    retrieval_recall_at_k,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "tests" / "fixtures" / "txodds_docs.yaml"
TASKS = ROOT / "tests" / "fixtures" / "golden" / "txodds_flywheel_tasks.jsonl"


def _read_key(name: str = "CLAUDE_API_KEY") -> str:
    import os

    if os.environ.get(name):
        return os.environ[name]
    candidates = [ROOT / ".env"]
    try:  # a linked worktree keeps its gitignored .env in the MAIN checkout
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(common).parent / ".env")
    except Exception:  # noqa: BLE001 - .env discovery is best-effort
        pass
    for p in candidates:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(rf"^{re.escape(name)}=(.*)$", line.strip())
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"{name} not found (environment or .env)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="runs per task per arm (variance)")
    ap.add_argument("--k", type=int, default=8, help="GECKO search top-k surfaced")
    ap.add_argument("--spec", default=str(SPEC), help="OpenAPI spec/docs path")
    ap.add_argument("--tasks", default=str(TASKS), help="golden tasks jsonl")
    ap.add_argument("--label", default="txodds", help="fixture label for the run")
    args = ap.parse_args()

    key = _read_key()
    import anthropic

    llm = anthropic.Anthropic(api_key=key)
    # Recorded-mode session — the pick+emit is the only spend; the API stays $0.
    client = AgentApiClient(
        args.spec, session=Session(jwt="recorded-mode", api_token="recorded-mode")
    )
    tasks = load_golden(pathlib.Path(args.tasks))

    # PASS 1 — comprehension only; capture where GECKO picked right but under-supplied a param.
    pass1 = evaluate_fcc(
        args.label, client, tasks, llm, model=BLURB_MODEL, k=args.k, n_runs=args.n
    )
    corrections = corrections_from_records(pass1)

    # PASS 2 — three arms, corrections re-injected into the GECKO+CORPUS tools.
    pass2 = evaluate_fcc(
        args.label,
        client,
        tasks,
        llm,
        model=BLURB_MODEL,
        k=args.k,
        n_runs=args.n,
        corrections=corrections,
    )

    print(f"# Flywheel FCC eval (model {BLURB_MODEL}, k={args.k}, N={args.n})")
    print(f"captured corrections: {len(corrections)}")
    for c in corrections:
        print(f"  - {c.tool_name}.{c.param} [{c.kind}] n={c.n_observed}: {c.hint}")
    print()
    # Read order: recall@k (the retrieval CEILING — FCC can't exceed it) -> FCC (what the
    # arm converted of that ceiling) -> hallucination-rate (invented-op noise). A retrieval
    # bottleneck is visible before any generation tuning.
    print(f"{'arm':<13} {'recall@k':>9} {'FCC':>6} {'halluc':>7}")
    for label, arm in (
        ("RAW", "raw"),
        ("GECKO", "gecko"),
        ("GECKO+CORPUS", "gecko_corpus"),
    ):
        print(
            f"{label:<13} {retrieval_recall_at_k(pass2, arm):>9.2f}"
            f" {fcc_rate(pass2, arm):>6.2f} {hallucination_rate(pass2, arm):>7.2f}"
        )
    print()
    print(f"comprehension lift {lift(pass2):+.2f} (GECKO vs RAW, FCC)")
    print(f"corpus lift        {lift_corpus(pass2):+.2f} (GECKO+CORPUS vs GECKO, FCC)")

    # Control-plane-clean substrate (shapes + booleans, never arg VALUES) to gitignored private/.
    out = ROOT / "private" / "flywheel-eval.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in pass2:
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
