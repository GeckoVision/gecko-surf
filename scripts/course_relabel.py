"""Build a BLIND relabelling worksheet for the course's 49 labelled questions.

WHY THIS EXISTS. The 49 questions were labelled against a units-only corpus. The
surface now serves 214 documents, so a page that answers better than the labelled
one scores as a miss: "how do I install uv" returns README and SETUP.md and is
counted wrong, because `unit0/get-settled` was the only candidate when the label
was written. Relabelling is the fix, and relabelling is also the most dangerous
thing you can do to an eval.

SO THE WORKSHEET IS BLIND, in three specific ways, each closing a way the person
relabelling could fit the labels to the ranker:

1. **No scores and no rank.** Candidates are shuffled with a seed derived from the
   question text, so the order is stable across runs and carries no signal.
2. **No indication of the current gold.** The existing label is not marked, and it
   is not moved to the front. If it is the best answer it has to win on its text.
3. **Candidates come from a UNION of arms**, not from the arm being measured, so a
   page the shipped ranker never returns is still on the sheet and can still be
   marked correct. A worksheet built from one ranker's output can only ever confirm
   that ranker.

The relabeller sees a question and some passages, and answers one question per
passage: does this answer it? That is a judgement about the course, not about us.

Output is JSONL, one row per question, ready to be filled in and then merged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gecko.doccorpus import DocIndex, load_pages  # noqa: E402
from gecko.providers import course_surface  # noqa: E402

DEV_CASES = Path("data") / "evals" / "coach.jsonl"
HELDOUT_CASES = Path("data") / "evals" / "coach-heldout.jsonl"

#: How many candidates a question gets. Enough that the right page is almost
#: certainly present, few enough that a person will actually read them all.
CANDIDATES = 12

#: The arms the candidate pool is drawn from. A union, on purpose: see the
#: docstring. Each entry is (label, kwargs for search_scored).
ARMS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("overlap", {}),
    ("overlap_floor", {"min_coverage": 0.5}),
    ("idf", {"idf": True}),
    ("idf_floor", {"idf": True, "min_coverage": 0.5}),
)

EXCERPT_CHARS = 320


def _cases(course_root: Path) -> list[dict[str, Any]]:
    rows = []
    for name in (DEV_CASES, HELDOUT_CASES):
        for line in (course_root / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                row["_set"] = name.name
                rows.append(row)
    return rows


def _pool(index: DocIndex, question: str) -> list[str]:
    """Candidate page ids from every arm, deduped, order not preserved."""
    found: set[str] = set()
    for _, kwargs in ARMS:
        for hit in index.search_scored(question, limit=CANDIDATES, **kwargs):
            found.add(hit.page_id)
    return sorted(found)


def worksheet(course_root: Path) -> list[dict[str, Any]]:
    pages = load_pages(
        course_root,
        suffixes=course_surface.SUFFIXES,
        include=course_surface.INCLUDE,
        exclude=course_surface.EXCLUDE,
    )
    index = DocIndex(pages)
    rows = []
    for case in _cases(course_root):
        question = case["question"]
        pool = _pool(index, question)[:CANDIDATES]
        # Seeded on the question, so two runs produce the same sheet and the order
        # is not a channel for rank.
        seed = int(hashlib.sha256(question.encode()).hexdigest()[:8], 16)
        random.Random(seed).shuffle(pool)
        rows.append(
            {
                "question": question,
                "set": case["_set"],
                "candidates": [
                    {
                        "page_id": page_id,
                        "title": index.pages[page_id].title,
                        "excerpt": index.pages[page_id].text[:EXCERPT_CHARS].strip(),
                        "answers": None,  # the relabeller fills this: true / false
                    }
                    for page_id in pool
                ],
            }
        )
    return rows


def merge(worksheet_path: Path) -> list[dict[str, Any]]:
    """Turn a FILLED worksheet into a labelled set, and refuse a half-filled one.

    Every candidate must carry a true/false judgement. A `null` left in means a
    question nobody finished, and a set with a silent hole in it measures the hole
    rather than the retrieval. This is the same refusal `blind_eval_score.py` makes
    for an unanswered rubric id, for the same reason.
    """
    rows = []
    unjudged = []
    for line in worksheet_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        expected = []
        for candidate in row["candidates"]:
            verdict = candidate.get("answers")
            if verdict is None:
                unjudged.append((row["question"], candidate["page_id"]))
            elif verdict is True:
                expected.append(candidate["page_id"])
        rows.append(
            {
                "question": row["question"],
                "expected_doc_ids": sorted(expected),
                "set": row["set"],
                # Kept so a reader can see the judgement was made over a real pool
                # rather than over whatever one ranker happened to return.
                "candidates_judged": len(row["candidates"]),
            }
        )
    if unjudged:
        sample = ", ".join(f"{q[:40]!r}/{p}" for q, p in unjudged[:3])
        raise SystemExit(
            f"refusing to merge: {len(unjudged)} candidate(s) carry no judgement ({sample}...). "
            "A label set with a hole in it measures the hole."
        )
    return rows


def compare(course_root: Path, labels_path: Path) -> dict[str, Any]:
    """Score the same arms under the ORIGINAL labels and the relabelled ones.

    Both, side by side, and the original set is never edited. Relabelling after
    seeing results is how an eval gets fitted to the code, so the frozen 49 stay
    frozen and act as the regression set; the new labels are a second opinion,
    not a replacement. A number that only improves under the labels written after
    the change is a number that has explained nothing.
    """
    pages_full = load_pages(
        course_root,
        suffixes=course_surface.SUFFIXES,
        include=course_surface.INCLUDE,
        exclude=course_surface.EXCLUDE,
    )
    pages_units = load_pages(
        course_root / "units" / "en", suffixes=(".mdx",), exclude=("quiz",)
    )
    indexes = {"full": DocIndex(pages_full), "units": DocIndex(pages_units)}

    original = {
        case["question"]: set(case["expected_doc_ids"]) for case in _cases(course_root)
    }
    relabelled = {}
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            relabelled[row["question"]] = set(row["expected_doc_ids"])

    def _fold(page_id: str) -> str:
        prefix = "units/en/"
        return page_id[len(prefix) :] if page_id.startswith(prefix) else page_id

    out: dict[str, Any] = {}
    for corpus, index in indexes.items():
        for label_name, gold in (("original", original), ("relabelled", relabelled)):
            hits = 0
            counted = 0
            for question, want in gold.items():
                if not want:
                    continue  # a question nothing answers cannot be hit
                counted += 1
                got = {
                    _fold(h.page_id)
                    for h in index.search_scored(
                        question, limit=3, min_coverage=0.5, idf=True
                    )
                }
                # The relabelled gold names FULL-corpus ids; the original names
                # units-relative ones. Fold both so the comparison is like for like.
                if {_fold(g) for g in want} & got:
                    hits += 1
            out[f"{corpus}/{label_name}"] = {"hits": hits, "total": counted}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-root", type=Path, required=False)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "retrieval" / "course-relabel-worksheet.jsonl",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="read a FILLED worksheet and write the labelled set",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="score the arms under BOTH the original and the relabelled sets",
    )
    args = parser.parse_args(argv)
    labels_path = args.out.with_name("course-labels.jsonl")

    if args.merge:
        labelled = merge(args.out)
        labels_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in labelled) + "\n",
            encoding="utf-8",
        )
        golds = sum(len(row["expected_doc_ids"]) for row in labelled)
        empty = sum(1 for row in labelled if not row["expected_doc_ids"])
        print(
            f"wrote {labels_path.relative_to(ROOT)}: {len(labelled)} questions, {golds} gold pages"
        )
        print(f"questions NO page in the pool answers: {empty}")
        return 0

    if args.compare:
        if args.course_root is None:
            raise SystemExit("--compare needs --course-root")
        if not labels_path.is_file():
            raise SystemExit(f"no {labels_path.name}; run --merge first")
        result = compare(args.course_root, labels_path)
        print(f"{'corpus / labels':26} {'hit@3':>12}")
        for key, value in result.items():
            rate = value["hits"] / value["total"] if value["total"] else 0.0
            print(f"{key:26} {value['hits']:>4}/{value['total']:<4} {rate:>6.0%}")
        return 0

    if args.course_root is None:
        raise SystemExit("--course-root is required to BUILD a worksheet")
    rows = worksheet(args.course_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    total = sum(len(row["candidates"]) for row in rows)
    print(
        f"wrote {args.out.relative_to(ROOT)}: {len(rows)} questions, {total} judgements"
    )
    print(
        "No scores, no rank, no current gold marked. Fill in `answers` per candidate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
