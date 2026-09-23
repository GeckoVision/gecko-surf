"""The retrieval scorecard, in version control, per archetype.

    uv run python scripts/retrieval_report.py            # write docs/retrieval/
    uv run python scripts/retrieval_report.py --check    # fail if it drifted

WHY THIS EXISTS. The number everyone quotes for this engine -- paraphrase
recall@8 of 0.04 ranker / 0.22 with_fallback -- lives in a gitignored note
(``private/2026-09-20-jev-intent-routing.md``), was produced by a throwaway loop
that no longer exists, and is guarded by no test. Nothing in CI would notice the
ranker regressing. A figure nobody can re-derive from the repository is not a
measurement, it is a memory.

Three things this fixes, and only these three:

1. **All four committed sets**, not the two in ``golden_baseline.py``. The note
   pooled txodds, pegana, privy and birdeye; the older runner knows about the
   first two, so it cannot reproduce the headline at all.
2. **Per archetype.** ``evaluate_golden`` puts ``archetype`` on every per-task
   card and then aggregates over all of them. The whole finding lives in the
   split: keyword_echo is ~1.00 and paraphrase is near zero, and an average over
   both says nothing about either.
3. **recall@8.** ``RECALL_KS`` is ``(1, 3, 5, 20)``, so the committed harness
   never computed the k the note reports. Ranks come back on the per-task cards,
   so this derives any k without touching the shared constant.

WHAT IT DOES NOT DO. It does not change the ranker, the readings, or the
fallback. It reports what is there today, so the next change has a before.

READ BOTH READINGS, ALWAYS. ``ranker`` counts genuine hits; ``with_fallback``
credits the never-empty 0/97 candidate's position. The second is not a ranker
number. And on ``paraphrase_no_overlap`` neither one can see a dense arm working,
because ``tests/test_golden_set.py`` enforces zero token overlap with the gold op,
``catalog.py`` therefore scores it 0, ``search.py`` flags every fused hit
``is_fallback``, and the ``ranker`` reading drops it. That chain is traced in
``docs/specs/2026-09-10-retrieval-review.md``. Quoting 0.04 as "the retrieval is
broken" repeats an artifact of the metric.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.access import Session, public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.evaluate import evaluate_golden, load_golden  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
OUT = ROOT / "docs" / "retrieval"

#: The ks this report answers. 8 is here because the number in circulation is
#: recall@8 and `RECALL_KS` does not contain it; the rest match the shared
#: constant so the two can be compared without arithmetic.
KS: tuple[int, ...] = (1, 3, 5, 8, 20)

#: Deep enough that a true rank of 20 is a rank and not a censored miss.
DEPTH = max(KS) + 10


def _two_token() -> Session:
    """The dummy session the golden sets were labelled under, so auth-gated ops surface."""
    return Session(jwt="recorded-mode", api_token="recorded-mode")


#: Spec and session per set, identical to `scripts/retrieval_arms_eval.py` and to
#: the pairing the sets were labelled under. Changing one without the other makes
#: every number here incomparable with every number before it.
CASES: dict[str, tuple[Path, Callable[[], Any]]] = {
    "txodds": (ROOT / "tests" / "fixtures" / "txodds_docs.yaml", _two_token),
    "pegana": (ROOT / "tests" / "fixtures" / "pegana_openapi.json", public_session),
    "privy": (GOLDEN / "privy_openapi.json", _two_token),
    "birdeye": (
        ROOT / "examples" / "birdeye_demo" / "spec" / "birdeye_openapi.json",
        _two_token,
    ),
}

READINGS = ("ranker", "with_fallback")


def _recall(ranks: Sequence[int | None], k: int) -> float:
    """Fraction of ranks at or above k. A miss is None, and it counts against you."""
    if not ranks:
        return 0.0
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def _by_archetype(cards: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Positive tasks grouped by archetype, both readings, every k.

    Out-of-scope tasks are excluded on purpose: they have no rank to recall, and
    folding their pass-rate into a recall column is how an average starts lying.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for card in cards:
        if not card["expect_ops"]:
            continue
        grouped.setdefault(card["archetype"], []).append(card)

    out: dict[str, dict[str, Any]] = {}
    for archetype, rows in sorted(grouped.items()):
        entry: dict[str, Any] = {"n": len(rows)}
        for reading in READINGS:
            ranks = [row[f"rank_{reading}"] for row in rows]
            entry[reading] = {str(k): round(_recall(ranks, k), 4) for k in KS}
        out[archetype] = entry
    return out


def build() -> dict[str, Any]:
    """Every set, scored once, reported per archetype and pooled."""
    per_set: dict[str, Any] = {}
    pooled: list[Mapping[str, Any]] = []

    for name, (spec, session) in CASES.items():
        client = AgentApiClient(str(spec), session=session())
        tasks = load_golden(GOLDEN / f"{name}_tasks.jsonl")
        card = evaluate_golden(client, tasks, limit=DEPTH)
        cards = list(card["per_task"])
        pooled += cards
        per_set[name] = {
            "ops": len(client.list_tools()),
            "n_positive": card["n_positive"],
            "n_oos": card["n_oos"],
            "n_via_fallback": card["n_via_fallback"],
            "oos_pass_rate": {
                reading: round(card["oos_pass_rate"][reading], 4)
                for reading in READINGS
            },
            "by_archetype": _by_archetype(cards),
        }

    return {
        "ks": list(KS),
        "depth": DEPTH,
        "sets": per_set,
        "pooled": {
            "n_positive": sum(1 for card in pooled if card["expect_ops"]),
            "by_archetype": _by_archetype(pooled),
        },
    }


def render(report: Mapping[str, Any]) -> str:
    """The same numbers as prose, so a reader is never handed one reading alone."""
    lines = [
        "# Retrieval scorecard",
        "",
        "Generated by `scripts/retrieval_report.py`. Do not hand-edit.",
        "",
        "`ranker` counts genuine hits. `with_fallback` credits the never-empty 0/97",
        "candidate's position and is **not** a ranker number. On `paraphrase_no_overlap`",
        "neither reading can see a dense arm working: the golden sets enforce zero token",
        "overlap with the gold op, so the lexical score is 0 by arithmetic and every fused",
        "hit is flagged low-confidence. See `docs/specs/2026-09-10-retrieval-review.md`.",
        "",
        "## Pooled, by archetype",
        "",
        "| archetype | n | "
        + " | ".join(f"ranker@{k}" for k in KS)
        + " | "
        + " | ".join(f"fb@{k}" for k in KS)
        + " |",
        "|---|---|" + "---|" * (2 * len(KS)),
    ]
    for archetype, entry in report["pooled"]["by_archetype"].items():
        cells = [f"{entry['ranker'][str(k)]:.2f}" for k in KS]
        cells += [f"{entry['with_fallback'][str(k)]:.2f}" for k in KS]
        lines.append(f"| {archetype} | {entry['n']} | " + " | ".join(cells) + " |")

    lines += ["", "## Per set", ""]
    for name, entry in report["sets"].items():
        lines.append(
            f"- **{name}** — {entry['ops']} usable ops · {entry['n_positive']} positive, "
            f"{entry['n_oos']} out-of-scope · {entry['n_via_fallback']} fallback-only · "
            f"out-of-scope pass {entry['oos_pass_rate']['ranker']:.2f} ranker / "
            f"{entry['oos_pass_rate']['with_fallback']:.2f} with-fallback"
        )
    return "\n".join(lines) + "\n"


def write(report: Mapping[str, Any], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    data = out / "scorecard.json"
    prose = out / "README.md"
    data.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    prose.write_text(render(report), encoding="utf-8")
    return [data, prose]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed report drifted"
    )
    args = parser.parse_args(argv)

    report = build()
    if not args.check:
        for path in write(report, args.out):
            print(f"wrote {path.relative_to(ROOT)}")
        print()
        print(render(report))
        return 0

    committed = args.out / "scorecard.json"
    if not committed.is_file():
        print(f"{committed.relative_to(ROOT)} is missing; run without --check")
        return 1
    if json.loads(committed.read_text(encoding="utf-8")) != json.loads(
        json.dumps(report)
    ):
        print(
            f"{committed.relative_to(ROOT)} is stale: the ranker moved, or the sets did."
        )
        print("re-run: uv run python scripts/retrieval_report.py")
        return 1
    print("retrieval scorecard current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
