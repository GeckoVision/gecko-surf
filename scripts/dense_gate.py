"""The EVIDENCE GATE for the dense hybrid arm — lexical baseline vs S0-enriched-lexical vs
S1-hybrid, on BOTH frozen golden fixtures. Populates ``gecko_rag.surface_docs`` (native
Atlas autoEmbed), builds the vector index, and scores recall@k/MRR + per-archetype + OOS,
plus a paired bootstrap CI (candidate − baseline reciprocal-rank over pooled positive tasks).

Thin transport: metrics live in ``gecko.evaluate``; fusion/dense in the package. Writes
``private/2026-07-01-dense-hybrid-results.md`` (gitignored benchmark, not payload).

    uv run python scripts/dense_gate.py

Reads MONGODB_URI from .env (never printed). Hyperparameters (RRF k=60, dense Z=1.0) are
pre-registered in ``gecko.fusion`` — NOT tuned here.
"""

from __future__ import annotations

import random
import re
import sys
import time
from pathlib import Path
from statistics import fmean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.access import Session, public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.dense import MongoAtlasDenseIndex  # noqa: E402
from gecko.enrich import load_pinned_blurbs, safe_blurb  # noqa: E402
from gecko.evaluate import RECALL_KS, evaluate_golden, load_golden  # noqa: E402
from gecko.fusion import RRF_K  # noqa: E402
from gecko.ingest import extract_operations, load_spec  # noqa: E402
from gecko.surfacedoc import surfacedoc_from_operation  # noqa: E402
from gecko.tools import tool_name  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
BLURBS = GOLDEN / "blurbs"
SCORE_DEPTH = max(RECALL_KS) + 10  # >= 20; uncensored above the deepest k

CASES = {
    "txodds": (
        ROOT / "tests" / "fixtures" / "txodds_docs.yaml",
        lambda: Session(jwt="recorded-mode", api_token="recorded-mode"),
    ),
    "pegana": (
        ROOT / "tests" / "fixtures" / "pegana_openapi.json",
        public_session,
    ),
}


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{re.escape(key)}=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    raise SystemExit(f"{key} not found in .env")


class _Retriever:
    """Adapts any ``search_scored``-shaped callable to what ``evaluate_golden`` calls, so the
    exact #37 metric scores every arm identically (only the retrieval fn differs)."""

    def __init__(self, fn):
        self._fn = fn

    def search_scored(self, query: str, limit: int):
        return self._fn(query, limit)


def _rr_by_task(card: dict) -> dict[str, float]:
    """Per positive task: reciprocal rank (1/rank, 0 on miss), keyed by goal."""
    out: dict[str, float] = {}
    for r in card["per_task"]:
        if not r["expect_ops"]:
            continue
        rank = r["rank"]
        out[r["goal"]] = (1.0 / rank) if rank else 0.0
    return out


def _archetype_recall5(card: dict) -> dict[str, tuple[int, int]]:
    agg: dict[str, list[int]] = {}
    for r in card["per_task"]:
        if not r["expect_ops"]:
            continue
        hit5 = 1 if (r["rank"] is not None and r["rank"] <= 5) else 0
        h, n = agg.get(r["archetype"], [0, 0])
        agg[r["archetype"]] = [h + hit5, n + 1]
    return {k: (v[0], v[1]) for k, v in agg.items()}


def _recall_line(block: dict) -> str:
    r = block["recall_at"]
    return (
        " · ".join(f"@{k} {r[k]:.2f}" for k in RECALL_KS) + f" · MRR {block['mrr']:.3f}"
    )


def _bootstrap_ci(deltas: list[float], iters: int = 10000, seed: int = 7):
    """95% percentile bootstrap CI over paired per-task deltas."""
    if not deltas:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(iters):
        means.append(fmean(rng.choices(deltas, k=n)))
    means.sort()
    lo = means[int(0.025 * iters)]
    hi = means[int(0.975 * iters)]
    return (fmean(deltas), lo, hi)


def main() -> None:
    uri = _env("MONGODB_URI")

    # --- build clients + upsert SurfaceDocs -------------------------------------------
    clients: dict[str, dict] = {}
    all_docs = []
    for name, (spec_path, session_factory) in CASES.items():
        spec = load_spec(str(spec_path))
        ops = extract_operations(spec)
        raw = load_pinned_blurbs(BLURBS / f"{name}.json")
        clean = {k: safe_blurb(v) for k, v in raw.items()}
        base = AgentApiClient(spec, session=session_factory())
        enriched = AgentApiClient(spec, session=session_factory(), blurbs=clean)
        clients[name] = {"base": base, "enriched": enriched}
        docs = [
            surfacedoc_from_operation(op, raw[tool_name(op)], surface_id=name)
            for op in ops
        ]
        all_docs += docs

    admin = MongoAtlasDenseIndex(uri, "txodds")
    admin.ensure_index()
    counts = admin.upsert(all_docs)
    print(
        f"[ok] upserted {len(all_docs)} SurfaceDocs into gecko_rag.surface_docs: {counts}"
    )
    # embeddings cover ALL docs (both surfaces); readiness = per-surface doc count embedded.
    n_docs_surface = {
        name: len([d for d in all_docs if d.surface_id == name]) for name in CASES
    }
    _wait_ready_uri(admin, uri, n_docs_surface)

    # --- score the three arms per fixture ---------------------------------------------
    report = _header()
    rr: dict[str, dict[str, float]] = {"A": {}, "B": {}, "C": {}}  # arm -> {goal -> RR}
    for name in CASES:
        base = clients[name]["base"]
        enriched = clients[name]["enriched"]
        dense = MongoAtlasDenseIndex(uri, name)

        arm_a = _Retriever(base.search_scored)
        arm_b = _Retriever(enriched.search_scored)
        arm_c = _Retriever(
            lambda q, lim, e=enriched, d=dense: e.search_hybrid_scored(
                q, lim, dense_index=d
            )
        )
        tasks = load_golden(GOLDEN / f"{name}_tasks.jsonl")
        card_a = evaluate_golden(arm_a, tasks, limit=SCORE_DEPTH)
        card_b = evaluate_golden(arm_b, tasks, limit=SCORE_DEPTH)
        card_c = evaluate_golden(arm_c, tasks, limit=SCORE_DEPTH)
        for arm, card in (("A", card_a), ("B", card_b), ("C", card_c)):
            rr[arm].update({f"{name}:{g}": v for g, v in _rr_by_task(card).items()})
        report += _fixture_block(name, base, enriched, card_a, card_b, card_c)
        dense.close()

    report += _paired_block(rr)
    dest = ROOT / "private" / "2026-07-01-dense-hybrid-results.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nwrote {dest}")
    admin.close()


def _wait_ready_uri(
    dense: MongoAtlasDenseIndex, uri: str, per_surface: dict[str, int]
) -> None:
    print("[..] waiting for autoEmbed index + embeddings (server-side)")
    deadline = time.time() + 420
    while time.time() < deadline:
        if dense.index_ready():
            counts = {}
            for name in per_surface:
                di = MongoAtlasDenseIndex(uri, name)
                counts[name] = len(di.search("liveness health status", 300))
                di.close()
            if all(counts[name] >= per_surface[name] for name in per_surface):
                print(f"[ok] all surfaces embedded: {counts}")
                return
            print(f"    embedded so far: {counts} (want {per_surface})")
        time.sleep(6)
    raise SystemExit("index/embeddings did not become ready within 420s")


def _header() -> str:
    return "\n".join(
        [
            "# Dense hybrid (MongoDB Atlas autoEmbed) — evidence gate",
            "",
            "Arms: **A** plain lexical (#37 baseline) · **B** S0 enriched-lexical (blurb in "
            "the overlap haystack) · **C** S1 hybrid = enriched-lexical + dense (Voyage "
            "`voyage-4-lite` autoEmbed), RRF-fused. recall@k/MRR over positive tasks at depth "
            f">= {SCORE_DEPTH}; OOS by the confidence-floor guard. Retrieval rank only — NOT "
            f"first-call-correct. Pre-registered: RRF k={RRF_K} (not tuned on the set).",
            "",
            "OOS floor is LEXICAL-ANCHORED: a fused hit is 'genuine' (above the floor) only if "
            "the lexical arm scored it > 0. Measured on `voyage-4-lite`, dense cosine scores "
            "sit in a ~0.005-wide band across the whole pool (OOS z-scores overlap in-scope "
            "z-scores), so a dense-score floor cannot separate OOS from a paraphrase; anchoring "
            "to lexical corroboration makes OOS(hybrid) >= OOS(lexical) by construction while "
            "dense still lifts paraphrase recall via RANK.",
            "",
        ]
    )


def _fixture_block(name, base, enriched, card_a, card_b, card_c) -> str:
    lines = [f"## {name}", f"- pool: {len(base.list_tools())} usable ops"]
    for arm, card in (
        ("A plain-lexical", card_a),
        ("B enriched-lexical", card_b),
        ("C hybrid", card_c),
    ):
        lines.append(
            f"- **{arm}**: {_recall_line(card['after_fix'])} · "
            f"OOS {card['oos_pass_rate']['after_fix']:.2f}"
        )
    lines.append("- recall@5 by archetype:")
    for arm, card in (("A", card_a), ("B", card_b), ("C", card_c)):
        cells = ", ".join(
            f"{a} {h}/{n}" for a, (h, n) in sorted(_archetype_recall5(card).items())
        )
        lines.append(f"    - {arm}: {cells}")
    # per-task rank table (A -> B -> C)
    lines += ["", "| goal | archetype | A | B | C |", "|---|---|---|---|---|"]
    a_by = {r["goal"]: r for r in card_a["per_task"]}
    b_by = {r["goal"]: r for r in card_b["per_task"]}
    for r in card_c["per_task"]:
        g = r["goal"]
        if not r["expect_ops"]:
            fa = "OOS✓" if a_by[g]["hit"] else "OOS✗"
            fb = "OOS✓" if b_by[g]["hit"] else "OOS✗"
            fc = "OOS✓" if r["hit"] else "OOS✗"
            lines.append(f"| {g} | {r['archetype']} | {fa} | {fb} | {fc} |")
        else:
            lines.append(
                f"| {g} | {r['archetype']} | {a_by[g]['rank']} | {b_by[g]['rank']} | {r['rank']} |"
            )
    return "\n".join(lines) + "\n\n"


def _paired_block(rr) -> str:
    goals = sorted(rr["A"])
    d_ba = [rr["B"][g] - rr["A"][g] for g in goals]
    d_ca = [rr["C"][g] - rr["A"][g] for g in goals]
    d_cb = [rr["C"][g] - rr["B"][g] for g in goals]
    out = [
        "## Paired test — reciprocal-rank delta (pooled positive tasks)",
        f"- n positive tasks pooled: {len(goals)}",
        "",
    ]
    for label, d in (
        ("B − A (S0 vs baseline)", d_ba),
        ("C − A (hybrid vs baseline)", d_ca),
        ("C − B (hybrid vs S0)", d_cb),
    ):
        mean, lo, hi = _bootstrap_ci(d)
        improved = sum(1 for x in d if x > 1e-9)
        worsened = sum(1 for x in d if x < -1e-9)
        gate = "PASS (CI lower > 0)" if lo > 0 else "no (CI includes 0)"
        out.append(
            f"- **{label}**: ΔRR mean {mean:+.3f}, 95% CI [{lo:+.3f}, {hi:+.3f}] → {gate}; "
            f"improved {improved} / worsened {worsened} tasks"
        )
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
