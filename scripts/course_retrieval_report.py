"""Gecko's lexical arm over a DOCUMENT corpus, measured against that corpus's own baseline.

    uv run python scripts/course_retrieval_report.py --course-root ../Dev3Pack-bootcamp-AI-Engineering
    uv run python scripts/course_retrieval_report.py --course-root ... --check

WHY THIS EXISTS. Step 5 of the retrieval plan points this engine at the Dev3Pack
course corpus. That corpus is not a neutral testbed: it already ships a measured
retriever (``bootcamp_agent.coach.rank`` — BM25 + title weight + one-per-page) with a
published number over 49 labelled questions written before any of it was tuned. So
the question is not "does Gecko return something" but "does Gecko's shipped lexical
arm beat a baseline somebody else fitted, on their data, at their k". If it loses,
that is the result, and it is worth more than a shipped server: it is an outside
measurement of our own engine, which is the whole CI-for-agents thesis.

WHAT MAKES THE COMPARISON FAIR, and it had to be checked rather than assumed:

* **The same 132 pages.** ``doccorpus.load_pages(units/en, suffixes=(".mdx",),
  exclude=("quiz",))`` reproduces the coach's corpus EXACTLY — same page ids, and
  all 132 page bodies sha256-identical. (The course loads its list from
  ``_toctree.yml``; the only files on disk that the toctree does not list are the 16
  quiz pages, which both sides exclude.)
* **The same 879 chunks** at ``max_chars=800`` with paragraph packing.
* **The same questions and the same k.** 25 dev + 24 held-out, hit@3 over distinct
  page ids — the coach's exact metric.

ONE KNOWN DIFFERENCE, stated rather than hidden: page TITLES. The coach takes the
title from ``_toctree.yml``; this loader takes it from the page's own H1, because a
generic document loader cannot depend on one course's table of contents. 55 of 132
match; the rest differ in wording ("Unit 1 — The environment" vs "Topic 1. The
environment"). Titles are ranked twice by the scorer, so this is a real difference
in both directions and neither side chose it.

WHAT IS NOT MEASURED HERE. Refusal. The course's labelled sets contain no
out-of-scope question, so the one metric surfcall could most usefully hand the
course — the out-of-scope pass rate — has no data on either side. ``DocIndex``
refuses by construction (no fallback prior) and a test pins that; a NUMBER for it
needs labels that do not exist yet.

This report is committed to ``docs/retrieval/course-scorecard.json``. It cannot run
in CI (the course corpus is a different repository), so ``--check`` is how a person
re-derives it, and the corpus fingerprint in the file is how a reader knows whether
the corpus they have is the corpus it was measured on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gecko.doccorpus import DocIndex, DocPage, load_pages  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "retrieval" / "course-scorecard.json"

#: Where the corpus and the labelled sets live inside the course repository.
UNITS = Path("units") / "en"
DEV_CASES = Path("data") / "evals" / "coach.jsonl"
HELDOUT_CASES = Path("data") / "evals" / "coach-heldout.jsonl"

#: The coach's published numbers (``src/bootcamp_agent/coach.py:110``), reproduced in
#: the course's own environment on 2026-09-23 with its own eval:
#: ``coach_eval.run(load_cases(...), documents=course_documents(), top_k=3)`` ->
#: 20/25 and 20/24. Quoted here so a reader of the scorecard does not have to.
BASELINE = {
    "source": "Dev3Pack-bootcamp-AI-Engineering/src/bootcamp_agent/coach.py:110",
    "arm": "BM25 + title_weight + one_per_page, top_k=3",
    "dev": {"hits": 20, "total": 25, "rate": 0.8},
    "heldout": {"hits": 20, "total": 24, "rate": round(20 / 24, 4)},
    "pooled": {"hits": 40, "total": 49, "rate": round(40 / 49, 4)},
}

#: Every k the report answers. 3 is the headline because it is the k the baseline
#: was measured at; 1 and 5 are here because a surface has to choose one and 5 is
#: what `scope.RETRIEVAL_MAX_TOOLS` would impose if the course were served today.
KS: tuple[int, ...] = (1, 3, 5)


@dataclass(frozen=True)
class Case:
    question: str
    expected: tuple[str, ...]


@dataclass(frozen=True)
class Variant:
    """One configuration of the document projector plus its post-rank policy."""

    name: str
    why: str
    max_chars: int = 800
    sections: bool = False
    oversize: str = "keep"
    one_per_source: bool = True
    gate: bool = False


#: The grid. Each row changes ONE thing against `shipped`, so a delta has a cause.
VARIANTS: tuple[Variant, ...] = (
    Variant(
        "shipped",
        "the arm the API surface ships, paragraph chunks at 800, one page per slot",
    ),
    Variant(
        "no_one_per_source",
        "the post-rank policy off: one long page may take every slot",
        one_per_source=False,
    ),
    Variant(
        "intent_gate_on",
        "the API surface's intent gate applied to prose: a hit must also match the "
        "title/tags/page-id surface",
        gate=True,
    ),
    Variant(
        "sections",
        "chunk at markdown headings first, and carry the heading path in the title",
        sections=True,
    ),
    Variant(
        "truncating_chunker",
        "the course chunker's own handling of an oversized block: cut it (drops "
        "table rows, keeps the header)",
        oversize="truncate",
    ),
    Variant(
        "chunks_400",
        "half the chunk budget — a SWEEP POINT, not a selection: on 25 dev questions "
        "one question is 4 points, so picking the best cell here would be fitting noise",
        max_chars=400,
    ),
    Variant(
        "whole_page",
        "no chunking at all: one page, one unit — the control that says what "
        "chunking bought",
        max_chars=10**6,
    ),
)


def load_cases(path: Path) -> list[Case]:
    cases: list[Case] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:  # pragma: no cover - bad input
            raise SystemExit(f"{path}:{number}: {error}") from error
        cases.append(Case(row["question"], tuple(row["expected_doc_ids"])))
    return cases


def _hit_counts(
    index: DocIndex, cases: Sequence[Case], variant: Variant
) -> tuple[dict[int, int], list[dict[str, Any]]]:
    """Hit rate at every k, plus the misses at the headline k.

    A hit is: at least one expected page id among the DISTINCT page ids returned —
    the coach's own metric, which scores whether the right PAGE was reached and says
    nothing about whether the passage answers the question. Both sides inherit that
    weakness, which is what makes the two numbers comparable at all.
    """
    misses: list[dict[str, Any]] = []
    per_k: dict[int, int] = dict.fromkeys(KS, 0)
    top = max(KS)
    for case in cases:
        hits = index.search_scored(
            case.question,
            limit=top,
            one_per_source=variant.one_per_source,
            gate=variant.gate,
        )
        pages = list(dict.fromkeys(hit.page_id for hit in hits))
        for k in KS:
            if set(pages[:k]) & set(case.expected):
                per_k[k] += 1
        if not set(pages[:3]) & set(case.expected):
            misses.append(
                {
                    "question": case.question,
                    "expected": list(case.expected),
                    "got": pages[:3],
                }
            )
    return per_k, misses


def _rates(counts: dict[int, int], total: int) -> dict[str, Any]:
    out: dict[str, Any] = {"n": total}
    for k in KS:
        out[f"hit@{k}"] = round(counts[k] / total, 4) if total else 0.0
        out[f"hits@{k}"] = counts[k]
    return out


_WORD = re.compile(r"[a-z0-9]+")


def _diagnose(index: DocIndex, misses: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Why the misses miss, as numbers rather than a hypothesis.

    The overlap scorer counts DISTINCT matched terms with no length normalisation
    and no IDF, so the unit with the broadest vocabulary has the most chances to
    overlap ANY question. If that is the mechanism, the chunks retrieved on a miss
    should be longer and wordier than the corpus average — and than the gold page's
    own chunks, which are the thing they beat.
    """

    def stats(texts: Sequence[str]) -> dict[str, float]:
        if not texts:
            return {"chars": 0.0, "distinct_words": 0.0, "n": 0}
        return {
            "chars": round(sum(len(t) for t in texts) / len(texts), 1),
            "distinct_words": round(
                sum(len(set(_WORD.findall(t.lower()))) for t in texts) / len(texts), 1
            ),
            "n": len(texts),
        }

    retrieved: list[str] = []
    gold: list[str] = []
    for miss in misses:
        retrieved += [
            hit.chunk.text for hit in index.search_scored(miss["question"], limit=3)
        ]
        wanted = set(miss["expected"])
        gold += [c.text for c in index.chunks if c.page_id in wanted]
    return {
        "what": "mean size of a chunk, corpus-wide vs retrieved on a dev miss vs on the gold page",
        "corpus": stats([c.text for c in index.chunks]),
        "retrieved_on_miss": stats(retrieved),
        "gold_page_chunks": stats(gold),
    }


def _fingerprint(pages: Sequence[DocPage]) -> dict[str, Any]:
    joined = "\n".join(f"{p.page_id}" for p in pages)
    return {
        "pages": len(pages),
        "chars": sum(len(p.text) for p in pages),
        "page_ids_sha256": hashlib.sha256(joined.encode()).hexdigest()[:16],
    }


def build(course_root: Path) -> dict[str, Any]:
    pages = load_pages(course_root / UNITS, suffixes=(".mdx",), exclude=("quiz",))
    dev = load_cases(course_root / DEV_CASES)
    heldout = load_cases(course_root / HELDOUT_CASES)

    variants: dict[str, Any] = {}
    misses_at_headline: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    for variant in VARIANTS:
        index = DocIndex(
            pages,
            max_chars=variant.max_chars,
            sections=variant.sections,
            oversize=variant.oversize,  # type: ignore[arg-type]
        )
        dev_counts, dev_misses = _hit_counts(index, dev, variant)
        heldout_counts, _ = _hit_counts(index, heldout, variant)
        pooled = {k: dev_counts[k] + heldout_counts[k] for k in KS}
        variants[variant.name] = {
            "why": variant.why,
            "config": {
                "max_chars": variant.max_chars,
                "sections": variant.sections,
                "oversize": variant.oversize,
                "one_per_source": variant.one_per_source,
                "intent_gate": variant.gate,
            },
            "chunks": len(index.chunks),
            "dev": _rates(dev_counts, len(dev)),
            "heldout": _rates(heldout_counts, len(heldout)),
            # The least noisy figure available: one question is 2 points here
            # instead of 4. Reported alongside, never instead of, the split —
            # the held-out set is the only uncontaminated one and pooling hides it.
            "pooled": _rates(pooled, len(dev) + len(heldout)),
        }
        if variant.name == "shipped":
            misses_at_headline = dev_misses
            diagnostics = _diagnose(index, dev_misses)
    return {
        "generated_by": "scripts/course_retrieval_report.py",
        "engine": "gecko.doccorpus.DocIndex over gecko.rankable (the shipped overlap arm)",
        "metric": "hit rate at k over DISTINCT page ids; a hit is any expected page id in the top k",
        "corpus": _fingerprint(pages)
        | {"root": "units/en", "loader_excludes": ["quiz"]},
        "cases": {
            "dev": {"file": str(DEV_CASES), "n": len(dev)},
            "heldout": {"file": str(HELDOUT_CASES), "n": len(heldout)},
        },
        "baseline": BASELINE,
        "variants": variants,
        "dev_misses_shipped": misses_at_headline,
        "miss_diagnostics": diagnostics,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"corpus: {report['corpus']['pages']} pages, "
        f"{report['corpus']['chars']} chars ({report['corpus']['page_ids_sha256']})",
        "",
        f"{'variant':<20} {'chunks':>7} {'dev@3':>10} {'held@3':>10} {'pooled@3':>10} {'dev@1':>7}",
    ]
    for name, row in report["variants"].items():
        lines.append(
            f"{name:<20} {row['chunks']:>7} "
            f"{row['dev']['hits@3']:>4}/{row['dev']['n']:<5} "
            f"{row['heldout']['hits@3']:>4}/{row['heldout']['n']:<5} "
            f"{row['pooled']['hits@3']:>4}/{row['pooled']['n']:<5} "
            f"{row['dev']['hit@1']:>7.2f}"
        )
    base = report["baseline"]
    lines.append(
        f"{'coach baseline':<20} {'-':>7} "
        f"{base['dev']['hits']:>4}/{base['dev']['total']:<5} "
        f"{base['heldout']['hits']:>4}/{base['heldout']['total']:<5} "
        f"{base['pooled']['hits']:>4}/{base['pooled']['total']:<5} {'-':>7}"
    )
    shipped = report["variants"]["shipped"]
    delta_dev = shipped["dev"]["hit@3"] - base["dev"]["rate"]
    delta_held = shipped["heldout"]["hit@3"] - base["heldout"]["rate"]
    delta_pooled = shipped["pooled"]["hit@3"] - base["pooled"]["rate"]
    lines += [
        "",
        f"shipped arm vs baseline: dev {delta_dev:+.2f}, held-out {delta_held:+.2f}, "
        f"pooled {delta_pooled:+.2f} "
        f"(one question is {1 / shipped['dev']['n']:.2f} dev, "
        f"{1 / shipped['heldout']['n']:.2f} held-out, "
        f"{1 / shipped['pooled']['n']:.2f} pooled)",
    ]
    return "\n".join(lines)


def markdown(report: dict[str, Any]) -> str:
    """The same numbers a reader will actually read. Generated, never hand-edited."""
    base = report["baseline"]
    shipped = report["variants"]["shipped"]
    diag = report["miss_diagnostics"]
    rows = [
        "| variant | chunks | dev@3 | held-out@3 | pooled@3 | what it changes |",
        "|---|---|---|---|---|---|",
    ]
    for name, row in report["variants"].items():
        rows.append(
            f"| `{name}` | {row['chunks']} | "
            f"{row['dev']['hits@3']}/{row['dev']['n']} ({row['dev']['hit@3']:.2f}) | "
            f"{row['heldout']['hits@3']}/{row['heldout']['n']} ({row['heldout']['hit@3']:.2f}) | "
            f"{row['pooled']['hits@3']}/{row['pooled']['n']} ({row['pooled']['hit@3']:.2f}) | "
            f"{row['why']} |"
        )
    rows.append(
        f"| **coach baseline** | 879 | "
        f"{base['dev']['hits']}/{base['dev']['total']} ({base['dev']['rate']:.2f}) | "
        f"{base['heldout']['hits']}/{base['heldout']['total']} ({base['heldout']['rate']:.2f}) | "
        f"{base['pooled']['hits']}/{base['pooled']['total']} ({base['pooled']['rate']:.2f}) | "
        f"{base['arm']} |"
    )
    corpus = report["corpus"]
    misses = "\n".join(
        f"- *{m['question']}* — wanted `{'` or `'.join(m['expected'])}`, got "
        f"`{'`, `'.join(m['got']) if m['got'] else 'nothing'}`"
        for m in report["dev_misses_shipped"]
    )
    return f"""# Gecko's lexical arm over a document corpus

Generated by `scripts/course_retrieval_report.py`. Do not hand-edit.

    uv run python scripts/course_retrieval_report.py --course-root <bootcamp clone>

The corpus is the Dev3Pack bootcamp: {corpus["pages"]} pages, {corpus["chars"]:,}
characters, page-id fingerprint `{corpus["page_ids_sha256"]}`. The questions and the
baseline are the course's own: {report["cases"]["dev"]["n"]} dev and
{report["cases"]["heldout"]["n"]} held-out labelled questions, the held-out set
written before the coach existed and never tuned against. Metric: hit rate at k over
distinct page ids — the coach's metric, which scores whether the right PAGE was
reached and says nothing about whether the passage answers the question.

{chr(10).join(rows)}

## The result: the shipped arm LOSES on dev, wins on held-out, loses pooled

{shipped["dev"]["hits@3"]}/{shipped["dev"]["n"]} against the baseline's
{base["dev"]["hits"]}/{base["dev"]["total"]} on dev — three questions worse.
{shipped["heldout"]["hits@3"]}/{shipped["heldout"]["n"]} against
{base["heldout"]["hits"]}/{base["heldout"]["total"]} held-out — one question better.
Pooled over all 49: {shipped["pooled"]["hits@3"]}/{shipped["pooled"]["n"]} against
{base["pooled"]["hits"]}/{base["pooled"]["total"]}. One question is 4 points on dev
and 2 pooled, so the honest reading is: **roughly level, slightly behind, and behind
on the half of the data that was available to tune against.** Gecko does not bring a
better ranker to this corpus. What it brings is refusal, the archetype split, and a
chunker that does not cut tables.

The dev misses say what the gap is, and it is measurable rather than a hypothesis.
The overlap scorer counts DISTINCT matched terms — no term frequency, no IDF, no
length normalisation — so the unit with the broadest vocabulary has the most chances
to overlap any question. The chunks it returns on a miss are bigger than the corpus
average in both dimensions:

| chunk population | mean chars | mean distinct words |
|---|---|---|
| the whole corpus | {diag["corpus"]["chars"]} | {diag["corpus"]["distinct_words"]} |
| retrieved on a dev miss | {diag["retrieved_on_miss"]["chars"]} | {diag["retrieved_on_miss"]["distinct_words"]} |
| the gold page it beat | {diag["gold_page_chunks"]["chars"]} | {diag["gold_page_chunks"]["distinct_words"]} |

That is the exact failure the course's own BM25 docstring names ("a page that
mentions everything once stops outranking the page that is about the question"), and
it is the mirror image of `docs/retrieval/README.md`, where our BM25F LOSES to
overlap on API surfaces. Short curated fields favour overlap; long prose favours
BM25. Neither arm is better in general — each is fitted to a field-length
distribution, and nobody had written that down.

### The dev misses, in full

{misses}

## Three policy findings, measured rather than argued

1. **The intent gate must be OFF for prose.** Turning the API surface's gate on
   collapses the corpus to
   {report["variants"]["intent_gate_on"]["pooled"]["hits@3"]}/{report["variants"]["intent_gate_on"]["pooled"]["n"]}
   pooled. A lesson does not restate in its heading what it teaches in its body.
2. **Chunking is worth little here, and one-per-page is worth about one question.**
   No chunking at all (`whole_page`) scores
   {report["variants"]["whole_page"]["pooled"]["hits@3"]}/{report["variants"]["whole_page"]["pooled"]["n"]};
   the shipped 800-character packer scores
   {shipped["pooled"]["hits@3"]}/{shipped["pooled"]["n"]}. Section chunking scores
   {report["variants"]["sections"]["pooled"]["hits@3"]}/{report["variants"]["sections"]["pooled"]["n"]}
   and is not selected.
3. **The truncating chunker scores IDENTICALLY** — which is a finding about the
   METRIC, not a licence to truncate. Cutting an oversized table cannot change which
   PAGE is reached, so a page-level hit rate is blind to it by construction. The
   course's packer drops ~34k characters of tables and lists this way, and no number
   on either side of this comparison can see it. An answer-containment label is the
   measurement that would.

## What is NOT measured here

**Refusal.** Neither labelled set contains an out-of-scope question, so the one
metric surfcall could most usefully hand the course — out-of-scope pass rate — has
no data on either side. `DocIndex` refuses by construction (it turns off the
never-empty prior) and `tests/test_doccorpus.py` pins that, but a property is not a
number.

**Anything about the dense arm.** The course corpus has no paraphrase archetype: a
question that shares no token with its page does not occur here. A dense win on this
corpus would not transfer to `paraphrase_no_overlap`, and a loss would not refute it.
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--course-root",
        type=Path,
        required=True,
        help="clone of the Dev3Pack bootcamp repository (the corpus and the labels)",
    )
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-derive and fail if it differs from the committed report",
    )
    args = parser.parse_args(argv)

    report = build(args.course_root)
    rendered = render(report)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    prose = markdown(report)
    doc = args.out.with_suffix(".md")

    if args.check:
        for path, expected in ((args.out, payload), (doc, prose)):
            if not path.is_file():
                print(f"missing {path}", file=sys.stderr)
                return 1
            if path.read_text(encoding="utf-8") != expected:
                print(
                    f"course retrieval scorecard DRIFTED: {path.name}", file=sys.stderr
                )
                print(rendered, file=sys.stderr)
                return 1
        print("course retrieval scorecard current")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload, encoding="utf-8")
    doc.write_text(prose, encoding="utf-8")
    print(rendered)
    print(f"\nwrote {args.out.relative_to(ROOT)} and {doc.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
