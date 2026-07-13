"""Agent-in-the-loop first-call-correct (FCC) eval — RAW spec dump vs Gecko comprehension.

Runs the ``gecko.fcc_eval`` harness against the two committed golden sets with a live cheap
model (Haiku), N times per task (Haiku is non-deterministic), and writes a Markdown report +
a control-plane-clean JSONL substrate to ``private/`` (gitignored — a benchmark log, not
payload). The pick+emit is the only spend; the API stays recorded/$0.

    uv run --extra fcc python scripts/fcc_eval.py             # both fixtures, N=3, k=8
    uv run --extra fcc python scripts/fcc_eval.py --n 5 --k 10

HONESTY: this is the COMPREHENSION lift (question-shaped, auth-hidden, retrieval-surfaced
tools vs the naive dump-every-op baseline), NOT the accumulated-corpus lift — no contributed
corpus exists yet. A thin edge on a well-documented API is a real finding.

Reads CLAUDE_API_KEY from the environment or a repo ``.env`` (never printed; not logged).
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

from gecko.access import Session, public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.enrich import BLURB_MODEL  # noqa: E402  (== "claude-haiku-4-5")
from gecko.evaluate import load_golden  # noqa: E402
from gecko.fcc_eval import (  # noqa: E402
    RunRecord,
    evaluate_fcc,
    fcc_rate,
    hallucination_rate,
    lift,
    per_archetype,
    positive,
    retrieval_recall_at_k,
    run_variance,
)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "golden"

CASES = {
    "txodds": (
        ROOT / "tests" / "fixtures" / "txodds_docs.yaml",
        lambda: Session(jwt="recorded-mode", api_token="recorded-mode"),
        "18-op regression-guard proxy (real ~97-op TxODDS spec not committed)",
    ),
    "pegana": (
        ROOT / "tests" / "fixtures" / "pegana_openapi.json",
        public_session,
        "41-op spec, 26 usable under public read — least-bad committed full-size pool",
    ),
}


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


def _component_rates(records: list[RunRecord], arm: str) -> dict[str, float]:
    """Decompose the headline FCC into its three gates over POSITIVE tasks — so a lift can be
    attributed (retrieval/pick vs plumbing vs arg-routing)."""
    rows = [r for r in positive(records) if r.arm == arm]
    n = len(rows) or 1
    return {
        "tool_correct": sum(r.tool_correct for r in rows) / n,
        "well_formed": sum(r.well_formed for r in rows) / n,
        "args_match": sum(r.args_match for r in rows) / n,
        "retrieval_hit": sum(r.retrieval_hit for r in rows) / n,
        "fcc": sum(r.fcc for r in rows) / n,
    }


def _oos_decline_rate(records: list[RunRecord], arm: str) -> tuple[int, int]:
    rows = [r for r in records if r.archetype == "out_of_scope" and r.arm == arm]
    return sum(r.fcc for r in rows), len(rows)


def _per_task_fcc(
    records: list[RunRecord],
) -> dict[tuple[str, str, str], dict[str, int]]:
    """(fixture, archetype, goal) -> {arm: fcc-successes-out-of-N}."""
    agg: dict[tuple[str, str, str], dict[str, int]] = {}
    counts: dict[tuple[str, str, str], dict[str, int]] = {}
    for r in records:
        key = (r.fixture, r.archetype, r.goal)
        agg.setdefault(key, {}).setdefault(r.arm, 0)
        counts.setdefault(key, {}).setdefault(r.arm, 0)
        agg[key][r.arm] += int(r.fcc)
        counts[key][r.arm] += 1
    return {k: {"agg": agg[k], "n": counts[k]} for k in agg}  # type: ignore[dict-item]


def _fixture_section(
    name: str, pool: int, note: str, recs: list[RunRecord]
) -> list[str]:
    raw_c = _component_rates(recs, "raw")
    gk_c = _component_rates(recs, "gecko")
    raw_recall, gk_recall = (
        retrieval_recall_at_k(recs, "raw"),
        retrieval_recall_at_k(recs, "gecko"),
    )
    raw_hall, gk_hall = (
        hallucination_rate(recs, "raw"),
        hallucination_rate(recs, "gecko"),
    )
    raw_m, raw_sd = run_variance(recs, "raw")
    gk_m, gk_sd = run_variance(recs, "gecko")
    raw_dec, raw_dn = _oos_decline_rate(recs, "raw")
    gk_dec, gk_dn = _oos_decline_rate(recs, "gecko")
    n_pos = len({r.goal for r in positive(recs)})
    n_oos = len({r.goal for r in recs if r.archetype == "out_of_scope"})
    out = [
        f"## {name}",
        f"- pool: {pool} usable ops · {note}",
        f"- positive tasks: {n_pos} · out-of-scope: {n_oos}",
        "",
        "| metric (positive tasks) | RAW | GECKO | lift |",
        "|---|---|---|---|",
        # Ceiling first: FCC can't exceed recall@k. Then the FCC conversion, then noise.
        f"| **recall@k (ceiling)** | {raw_recall:.2f} | {gk_recall:.2f} | — |",
        f"| tool_correct | {raw_c['tool_correct']:.2f} | {gk_c['tool_correct']:.2f} | {gk_c['tool_correct'] - raw_c['tool_correct']:+.2f} |",
        f"| well_formed | {raw_c['well_formed']:.2f} | {gk_c['well_formed']:.2f} | {gk_c['well_formed'] - raw_c['well_formed']:+.2f} |",
        f"| args_match | {raw_c['args_match']:.2f} | {gk_c['args_match']:.2f} | {gk_c['args_match'] - raw_c['args_match']:+.2f} |",
        f"| **FCC (converted)** | {raw_c['fcc']:.2f} | {gk_c['fcc']:.2f} | **{gk_c['fcc'] - raw_c['fcc']:+.2f}** |",
        f"| hallucination | {raw_hall:.2f} | {gk_hall:.2f} | — |",
        "",
        f"- FCC across runs — RAW {raw_m:.2f} ± {raw_sd:.2f} · GECKO {gk_m:.2f} ± {gk_sd:.2f}",
        f"- out-of-scope decline (correct): RAW {raw_dec}/{raw_dn} · GECKO {gk_dec}/{gk_dn}",
        "",
        "### FCC by archetype (positive)",
        "| archetype | RAW | GECKO | lift |",
        "|---|---|---|---|",
    ]
    ra, ga = per_archetype(recs, "raw"), per_archetype(recs, "gecko")
    for a in sorted(set(ra) | set(ga)):
        if a == "out_of_scope":
            continue
        out.append(
            f"| {a} | {ra.get(a, 0):.2f} | {ga.get(a, 0):.2f} | {ga.get(a, 0) - ra.get(a, 0):+.2f} |"
        )
    out += [
        "",
        "### Per-task (FCC successes / N runs)",
        "| archetype | goal | RAW | GECKO | ret_hit(G) |",
        "|---|---|---|---|---|",
    ]
    pt = _per_task_fcc(recs)
    ret = {
        (r.fixture, r.archetype, r.goal): r.retrieval_hit
        for r in recs
        if r.arm == "gecko"
    }
    for (fx, arch, goal), v in sorted(pt.items(), key=lambda kv: (kv[0][1], kv[0][2])):
        rw = f"{v['agg'].get('raw', 0)}/{v['n'].get('raw', 0)}"
        gk = f"{v['agg'].get('gecko', 0)}/{v['n'].get('gecko', 0)}"
        out.append(
            f"| {arch} | {goal[:52]} | {rw} | {gk} | {ret.get((fx, arch, goal))} |"
        )
    out.append("")
    return out


def run(n_runs: int, k: int) -> tuple[str, list[RunRecord]]:
    key = _read_key()
    import anthropic

    llm = anthropic.Anthropic(api_key=key)
    all_recs: list[RunRecord] = []
    sections: list[str] = []
    for name, (spec, session_factory, note) in CASES.items():
        client = AgentApiClient(str(spec), session=session_factory())
        tasks = load_golden(GOLDEN / f"{name}_tasks.jsonl")
        recs = evaluate_fcc(
            name, client, tasks, llm, model=BLURB_MODEL, k=k, n_runs=n_runs
        )
        all_recs += recs
        sections += _fixture_section(name, len(client.list_tools()), note, recs)

    pooled_raw = fcc_rate(all_recs, "raw")
    pooled_gk = fcc_rate(all_recs, "gecko")
    header = [
        "# FCC eval — RAW OpenAPI dump vs Gecko comprehension",
        "",
        f"Generated by `scripts/fcc_eval.py` (model `{BLURB_MODEL}`, k={k}, N={n_runs}).",
        "",
        "**This is the COMPREHENSION lift, not the corpus lift.** RAW = every operation dumped",
        "verbatim (raw operationId + summary/description, all params incl. auth, auth still",
        "required) — the naive tool set a DIY builder / coding-agent one-shot produces. GECKO =",
        "`client.search(goal)` → top-k question-shaped, auth-hidden defs. No contributed",
        "correctness corpus exists yet; this measures only whether Gecko's *shaping* lifts",
        "first-call-correct over the raw spec. Retrieval recall/MRR (#37/#38) is the retrieval",
        "sub-metric; this is the joint agent-in-the-loop metric.",
        "",
        "## Pooled headline (positive tasks, both fixtures)",
        "Read order: recall@k (retrieval ceiling) → FCC (converted) → hallucination-rate.",
        f"- **recall@k GECKO {retrieval_recall_at_k(all_recs, 'gecko'):.2f} (ceiling) · "
        f"RAW FCC {pooled_raw:.2f} · GECKO FCC {pooled_gk:.2f} · lift {pooled_gk - pooled_raw:+.2f} · "
        f"halluc RAW {hallucination_rate(all_recs, 'raw'):.2f} / GECKO {hallucination_rate(all_recs, 'gecko'):.2f}**",
        "- per-fixture lift: "
        + " · ".join(
            f"{n} {lift([r for r in all_recs if r.fixture == n]):+.2f}" for n in CASES
        ),
        "",
    ]
    return "\n".join(header + sections), all_recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="runs per task per arm (variance)")
    ap.add_argument("--k", type=int, default=8, help="GECKO search top-k surfaced")
    args = ap.parse_args()

    report, records = run(args.n, args.k)
    out_md = ROOT / "private" / "2026-07-02-fcc-eval-results.md"
    out_jsonl = ROOT / "private" / "2026-07-02-fcc-eval.jsonl"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report, encoding="utf-8")
    with out_jsonl.open("w", encoding="utf-8") as fh:
        for r in records:  # control-plane clean: shapes + booleans, never arg VALUES
            fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
    print(report)
    print(f"\nwrote {out_md}\nwrote {out_jsonl}")


if __name__ == "__main__":
    main()
