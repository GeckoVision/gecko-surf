"""Retrieval-arm comparison — the >50-op scale-gate falsifier (retrieval spec §4).

Compares four lexical/hybrid arms on the frozen golden sets, reusing `evaluate_golden`
(the ranker's own recall@k/MRR scorecard) so no hand-rolled metric judges the result:

  * A — token-overlap, baseline `[a-z0-9]+` tokenizer (camelCase glued).
  * B — token-overlap, shipped camelCase identifier tokenizer.
  * C — Okapi BM25F (IDF + TF-saturation + length-norm + OpenAPI-remapped field weights),
        identifier tokenizer kept.
  * D — BM25 + dense/RRF rerank (reuses `gecko.dense` + `gecko.fusion`). The dense arm needs
        Atlas `$vectorSearch` autoEmbed (Voyage) — NOT offline/$0 — so it is spec'd-but-SKIPPED
        here and reported as such (never faked). Enable via `--dense` with MONGODB_URI + the
        embedded `gecko_rag.surface_docs` view populated by `scripts/dense_gate.py`.

Three golden sets: txodds (18 usable), pegana (26 usable), privy (159 usable — the first real
>50-op set, so the scale gate can finally fire). Arms A/B/C run fully offline/$0.

GATE (retrieval spec §2): enable the dense/RRF rerank (arm D) only where BM25 (arm C) alone
leaves recall@3 < 0.8. This harness reports whether that condition fires per set.

    uv run python scripts/retrieval_arms_eval.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko import catalog  # noqa: E402
from gecko.access import Session, public_session  # noqa: E402
from gecko.catalog import BM25Index  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import RECALL_KS, evaluate_golden, load_golden  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
SCORE_DEPTH = max(RECALL_KS) + 10  # >= 20, uncensored above the deepest k
GATE_RECALL3 = 0.8  # dense/RRF (arm D) fires only where BM25 leaves recall@3 below this


# Same spec↔session pairing as the frozen golden labeling (test_golden_set / dense_gate):
# a dummy two-token session so auth-gated ops surface; pegana is a public read surface.
def _two_token() -> Session:
    return Session(jwt="recorded-mode", api_token="recorded-mode")


CASES: dict[str, tuple[Path, Callable[[], Any]]] = {
    "txodds": (ROOT / "tests" / "fixtures" / "txodds_docs.yaml", _two_token),
    "pegana": (ROOT / "tests" / "fixtures" / "pegana_openapi.json", public_session),
    "privy": (GOLDEN / "privy_openapi.json", _two_token),
}

_SHIPPED_TOKENS = catalog._tokens


def _baseline_tokens(text: str) -> set[str]:
    """Arm A tokenizer — pre-camelCase: lowercase then `[a-z0-9]+` (identifiers glued)."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


class _Retriever:
    """Adapts a `search_scored`-shaped callable to what `evaluate_golden` calls, so the exact
    same recall metric scores every arm — only the retrieval function differs."""

    def __init__(self, fn: Callable[[str, int], list[Any]]):
        self._fn = fn

    def search_scored(self, query: str, limit: int) -> list[Any]:
        return self._fn(query, limit)


def _bm25_retriever(bm25: BM25Index, usable: set[str]) -> _Retriever:
    """BM25 arm as a client-shaped retriever: over-fetch, apply the auth filter AFTER ranking
    (mirrors `AgentApiClient.search_scored`), then truncate — so an auth-hidden op never
    steals a top-k slot from a usable one."""

    def fn(query: str, limit: int) -> list[Any]:
        hits = bm25.search_scored(query, limit + 20)
        return [h for h in hits if h.name in usable][:limit]

    return _Retriever(fn)


def _card(retriever: _Retriever, tasks: list[Any]) -> dict[str, Any]:
    return evaluate_golden(retriever, tasks, limit=SCORE_DEPTH)


def run() -> dict[str, Any]:
    """Return `{set: {"n": usable_ops, "arms": {arm: card}}}` for arms A/B/C, offline/$0.

    Arm A/B swap only the module-global tokenizer around the SAME client (the ranker reads
    `catalog._tokens` at search time); arm C is BM25 over that client's catalog entries. The
    global tokenizer is always restored (finally) so the harness leaves no residue."""
    out: dict[str, Any] = {}
    for name, (spec, session_factory) in CASES.items():
        client = AgentApiClient(str(spec), session=session_factory())
        usable = {t["name"] for t in client.list_tools()}
        tasks = load_golden(GOLDEN / f"{name}_tasks.jsonl")
        try:
            catalog._tokens = _baseline_tokens
            card_a = _card(_Retriever(client.search_scored), tasks)
            catalog._tokens = _SHIPPED_TOKENS
            card_b = _card(_Retriever(client.search_scored), tasks)
        finally:
            catalog._tokens = _SHIPPED_TOKENS
        bm25 = BM25Index(client.catalog.entries)
        card_c = _card(_bm25_retriever(bm25, usable), tasks)
        out[name] = {
            "n": len(usable),
            "arms": {"A": card_a, "B": card_b, "C": card_c},
        }
    return out


def _recall(card: dict[str, Any], k: int) -> float:
    return float(card["after_fix"]["recall_at"][k])


def gate_fires(card_c: dict[str, Any], n_ops: int) -> bool:
    """The dense/RRF (arm D) gate: usable_ops > 50 AND BM25 recall@3 < 0.8 (retrieval spec §2)."""
    return n_ops > 50 and _recall(card_c, 3) < GATE_RECALL3


def format_report(results: dict[str, Any]) -> str:
    lines = [
        "# Retrieval arms — recall@k / MRR across golden sets",
        "",
        "Arms: **A** overlap+baseline-tokenizer · **B** overlap+camelCase · **C** BM25F "
        "(OpenAPI-remapped weights) · **D** BM25+dense/RRF (SKIPPED offline — needs Atlas "
        "autoEmbed). recall over positive tasks (after-fix), OOS by the confidence-floor guard.",
        "",
    ]
    for name, r in results.items():
        n = r["n"]
        lines.append(f"## {name} ({n} usable ops)")
        header = "| arm | " + " | ".join(f"r@{k}" for k in RECALL_KS) + " | MRR | OOS |"
        lines.append(header)
        lines.append("|" + "---|" * (len(RECALL_KS) + 3))
        for arm in ("A", "B", "C"):
            card = r["arms"][arm]
            cells = " | ".join(f"{_recall(card, k):.2f}" for k in RECALL_KS)
            mrr = card["after_fix"]["mrr"]
            oos = card["oos_pass_rate"]["after_fix"]
            lines.append(f"| {arm} | {cells} | {mrr:.3f} | {oos:.2f} |")
        lines.append("| D | — | — | — | — | (skipped: Atlas autoEmbed, not offline) |")
        fires = gate_fires(r["arms"]["C"], n)
        lines.append(
            f"- dense/RRF gate (ops>50 AND BM25 recall@3<{GATE_RECALL3}): "
            f"**{'FIRES' if fires else 'does not fire'}** "
            f"(ops={n}, BM25 recall@3={_recall(r['arms']['C'], 3):.2f})"
        )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    results = run()
    report = format_report(results)
    print(report)
    dest = ROOT / "private" / "2026-07-09-retrieval-arms.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(report, encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
