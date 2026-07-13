"""Self-heal agent-loop eval — does the offline sandbox TEACH a correlated multi-step flow?
(self-healing-loop plan, Task 5.2)

Sibling of ``scripts/flywheel_eval.py``. Runs a bounded Anthropic tool-use loop over a small
correlated escrow golden (deposit-before-withdraw) in probe mode ($0/synthetic — no wire),
and prints two arms:

  - BASELINE  — deposit + withdraw tools; failed calls return the error BODY only.
  - SELF-HEAL — the same tools PLUS ``query_docs``; failed calls return signals + remediation.

The go/no-go: does SELF-HEAL raise multi-step completion over BASELINE? A thin or zero lift
is a REAL finding (Pattern B), reported plainly — this script never fabricates a pass.

    uv run --extra fcc python scripts/selfheal_eval.py            # N=3, max-iters=6
    uv run --extra fcc python scripts/selfheal_eval.py --n 4 --max-iters 8

Reads CLAUDE_API_KEY from the environment or a repo ``.env`` (never printed; not logged). The
offline test (``tests/test_selfheal_eval.py``) is the gate — this runner is the final check.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.enrich import BLURB_MODEL  # noqa: E402  (== "claude-haiku-4-5")
from gecko.fcc_eval import hallucination_rate  # noqa: E402  (the reused Phase-0 metric)
from gecko.selfheal_eval import (  # noqa: E402
    escrow_client,
    escrow_operations,
    escrow_tasks,
    heal_rate,
    mean_iters_to_heal,
    multi_step_lift,
    multi_step_success_rate,
    run_eval,
    success_variance,
)

ROOT = Path(__file__).resolve().parent.parent


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
    ap.add_argument("--max-iters", type=int, default=6, help="tool-use loop budget")
    args = ap.parse_args()

    key = _read_key()
    import anthropic

    llm = anthropic.Anthropic(api_key=key)

    # Probe-only surface: NO live transport is wired, so nothing here CAN reach the wire.
    # Guard the invariant explicitly — if a real request is ever attempted, fail loudly.
    def _forbid_wire(req: object) -> tuple[int, object]:
        raise SystemExit("probe eval attempted a live call — invariant #3 violated")

    client = escrow_client()
    client._live_transport = _forbid_wire  # type: ignore[attr-defined]
    ops = escrow_operations()
    tasks = escrow_tasks()

    records = run_eval(
        llm,
        client,
        ops,
        tasks,
        model=BLURB_MODEL,
        n_runs=args.n,
        max_iters=args.max_iters,
    )

    print(
        f"# Self-heal agent-loop eval (model {BLURB_MODEL}, "
        f"tasks={len(tasks)}, N={args.n}, max_iters={args.max_iters})"
    )
    print(f"{'arm':<10} {'multi_step':>11} {'healed':>7} {'halluc':>7} {'iters':>6}")
    for arm in ("baseline", "selfheal"):
        mean, stdev = success_variance(records, arm)
        iters = mean_iters_to_heal(records, arm)
        print(
            f"{arm:<10} {multi_step_success_rate(records, arm):>11.2f}"
            f" {heal_rate(records, arm):>7.2f} {hallucination_rate(records, arm):>7.2f}"
            f" {('%.1f' % iters) if iters is not None else '   -':>6}"
            f"   (per-run mean {mean:.2f} ± {stdev:.2f})"
        )
    print()
    lift = multi_step_lift(records)
    verdict = "LIFT" if lift > 0 else ("NO LIFT" if lift == 0 else "REGRESSION")
    print(f"multi-step lift {lift:+.2f} (SELF-HEAL vs BASELINE)  ->  {verdict}")

    # Control-plane-clean substrate (booleans + counts, never arg VALUES) to gitignored private/.
    out = ROOT / "private" / "selfheal-eval.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
