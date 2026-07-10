"""Offline recall@k arm-comparison for the camelCase-operationId tokenizer fix.

The Pattern-B falsifier behind the `catalog.py` tokenizer change: measures retrieval
recall@k over the committed golden sets under two arms, reusing `evaluate_golden`
unchanged — so the ranker's own scorecard, not a hand-rolled metric, judges the fix.

  * arm A — baseline: the old `[a-z0-9]+` lowercase tokenizer (camelCase glued).
  * arm B — +camelCase: the shipped `catalog._tokens` (identifiers split on
    camelCase / digit / separator boundaries).

Deterministic, $0, no network, no vectors. Reports genuine-hit recall@k (fallback
candidates dropped — `evaluate_golden`'s `before_fix` block) because that is exactly the
signal the tokenizer moves: a glued operationId scored 0 → fell to the never-empty
fallback; splitting it recovers a GENUINE lexical hit. Adopt rule below.

    uv run python scripts/tokenizer_eval.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko import catalog  # noqa: E402
from gecko.access import Session, public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import RECALL_KS, evaluate_golden, load_golden  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
SCORE_DEPTH = max(RECALL_KS) + 10  # >= 20, uncensored above the deepest k

# Same spec↔golden pairing as scripts/golden_baseline.py (single committed source of truth).
CASES: dict[str, tuple[Path, Callable[[], Session]]] = {
    "txodds": (
        ROOT / "tests" / "fixtures" / "txodds_docs.yaml",
        lambda: Session(jwt="recorded-mode", api_token="recorded-mode"),
    ),
    "pegana": (ROOT / "tests" / "fixtures" / "pegana_openapi.json", public_session),
}

_FIXED_TOKENS = catalog._tokens  # arm B — the shipped tokenizer


def _baseline_tokens(text: str) -> set[str]:
    """Arm A — the pre-fix tokenizer: lowercase then `[a-z0-9]+`, camelCase glued."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _genuine_recall(client: AgentApiClient, tasks: list[Any]) -> dict[int, float]:
    """recall@k over GENUINE lexical hits (fallback candidates dropped) — the tokenizer's
    own signal. `evaluate_golden`'s `before_fix` block is exactly that projection."""
    card = evaluate_golden(client, tasks, limit=SCORE_DEPTH)
    return {k: card["before_fix"]["recall_at"][k] for k in RECALL_KS}


def run() -> dict[str, Any]:
    """Return `{set: {"A": recall@k, "B": recall@k}}` for both arms, offline/$0.

    Both arms share one constructed client per set; only the module-global tokenizer is
    swapped between measurements (the ranker reads `catalog._tokens` at search time), so
    the arms differ ONLY in the tokenizer — nothing else moves.
    """
    out: dict[str, Any] = {}
    for name, (spec, session_factory) in CASES.items():
        client = AgentApiClient(str(spec), session=session_factory())
        tasks = load_golden(GOLDEN / f"{name}_tasks.jsonl")
        try:
            catalog._tokens = _baseline_tokens  # arm A
            arm_a = _genuine_recall(client, tasks)
            catalog._tokens = _FIXED_TOKENS  # arm B
            arm_b = _genuine_recall(client, tasks)
        finally:
            catalog._tokens = _FIXED_TOKENS  # never leave the global patched
        out[name] = {"A": arm_a, "B": arm_b, "n": len(client.list_tools())}
    return out


def decide(results: dict[str, Any]) -> tuple[bool, bool]:
    """(no_regression, rose). Adopt iff recall@k never regresses on any set/k AND rises
    somewhere. A strict superset tokenizer must never regress; a lift is the bonus."""
    no_regression = True
    rose = False
    for r in results.values():
        for k in RECALL_KS:
            if r["B"][k] < r["A"][k] - 1e-9:
                no_regression = False
            if r["B"][k] > r["A"][k] + 1e-9:
                rose = True
    return no_regression, rose


def format_report(results: dict[str, Any]) -> str:
    lines = ["# camelCase tokenizer — recall@k before/after (genuine hits)", ""]
    for name, r in results.items():
        lines.append(f"## {name} ({r['n']} usable ops)")
        lines.append("| arm | " + " | ".join(f"@{k}" for k in RECALL_KS) + " |")
        lines.append("|" + "---|" * (len(RECALL_KS) + 1))
        for arm, label in (("A", "A baseline"), ("B", "B +camelCase")):
            cells = " | ".join(f"{r[arm][k]:.2f}" for k in RECALL_KS)
            lines.append(f"| {label} | {cells} |")
        deltas = " · ".join(f"@{k} {r['B'][k] - r['A'][k]:+.2f}" for k in RECALL_KS)
        lines.append(f"- Δ(B−A): {deltas}")
        lines.append("")
    no_regression, rose = decide(results)
    verdict = (
        "ADOPT"
        if (no_regression and rose)
        else (
            "ADOPT (no-regression; flat on these sets — lift is scale/thin-summary bound)"
            if no_regression
            else "REJECT — a set regressed"
        )
    )
    lines.append(f"**Pass rule (adopt iff rises AND no regression): {verdict}**")
    lines.append(f"- no_regression={no_regression} · rose={rose}")
    return "\n".join(lines)


def main() -> None:
    print(format_report(run()))


if __name__ == "__main__":
    main()
