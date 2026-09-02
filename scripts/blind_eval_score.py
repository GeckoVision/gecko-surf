"""Score a blind-eval result against its rubric and print the coordinator's checklist.

    uv run python scripts/blind_eval_score.py evals/blind/results/<date>-<mission>-<runner>.json

The output is the thing a person acts on: ``8/10``, then the failed checks with the
lane that owns each, then the friction lines. Deterministic: same input, same text.
The rubric is the mission's ``missions/<mission>.rubric.json``; a result that skips a
rubric id or answers an id the rubric does not have is an error, not a score.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESULTS = ("pass", "fail", "not_reached")


class BlindEvalError(Exception):
    """The result and the rubric do not line up; nothing was scored."""


def load_rubric(mission: str, missions_dir: Path) -> dict[str, Any]:
    path = missions_dir / f"{mission}.rubric.json"
    if not path.exists():
        raise BlindEvalError(f"no rubric for mission {mission!r} at {path}")
    return json.loads(path.read_text())


def score(result: dict[str, Any], rubric: dict[str, Any]) -> dict[str, Any]:
    """Join the result's answers to the rubric's checks. Every id, exactly once."""
    if result.get("mission") != rubric.get("mission"):
        raise BlindEvalError(
            f"result is for {result.get('mission')!r}, rubric for {rubric.get('mission')!r}"
        )
    by_id = {c["id"]: c for c in rubric["checks"]}
    answers: dict[str, dict[str, Any]] = {}
    for answer in result.get("checks", []):
        cid = answer.get("id")
        if cid not in by_id:
            raise BlindEvalError(
                f"result answers {cid!r}, which the rubric does not have"
            )
        if cid in answers:
            raise BlindEvalError(f"result answers {cid!r} twice")
        if answer.get("result") not in RESULTS:
            raise BlindEvalError(f"{cid}: result must be one of {RESULTS}")
        answers[cid] = answer
    missing = [cid for cid in by_id if cid not in answers]
    if missing:
        raise BlindEvalError(f"result does not answer: {', '.join(missing)}")

    rows = []
    for check in rubric["checks"]:
        answer = answers[check["id"]]
        rows.append(
            {
                "id": check["id"],
                "question": check["question"],
                "lane": check["lane"],
                "result": answer["result"],
                "evidence": answer.get("evidence", ""),
            }
        )
    passed = sum(1 for r in rows if r["result"] == "pass")
    failed = [r for r in rows if r["result"] == "fail"]
    not_reached = [r for r in rows if r["result"] == "not_reached"]
    return {
        "mission": result["mission"],
        "date": result.get("date", ""),
        "runner": result.get("runner", ""),
        "passed": passed,
        "total": len(rows),
        "rows": rows,
        "failed": failed,
        "not_reached": not_reached,
        "friction": list(result.get("friction", [])),
        "verdict": result.get("verdict", ""),
        "single_most_valuable_change": result.get("single_most_valuable_change", ""),
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['mission']}: {summary['passed']}/{summary['total']}",
        "",
        f"Run: {summary['date']} by {summary['runner']}.",
        "",
    ]
    if summary["failed"]:
        lines.append("## The work (failed checks, with the lane that owns each)")
        lines.append("")
        for row in summary["failed"]:
            lines.append(f"- [ ] `{row['id']}` ({row['lane']}): {row['question']}")
            if row["evidence"]:
                lines.append(f"      evidence: {row['evidence']}")
        lines.append("")
    else:
        lines.append("No failed checks.")
        lines.append("")
    if summary["not_reached"]:
        lines.append("## Not reached (holes in the run, not failures)")
        lines.append("")
        for row in summary["not_reached"]:
            lines.append(f"- `{row['id']}` ({row['lane']}): {row['question']}")
        lines.append("")
    lines.append("## Passed")
    lines.append("")
    for row in summary["rows"]:
        if row["result"] == "pass":
            lines.append(f"- [x] `{row['id']}`: {row['evidence'] or row['question']}")
    lines.append("")
    if summary["friction"]:
        lines.append(
            "## Friction reported (not scored; candidates for the next rubric)"
        )
        lines.append("")
        for item in summary["friction"]:
            lines.append(f"- {item}")
        lines.append("")
    if summary["single_most_valuable_change"]:
        lines.append(
            f"Tester's single most valuable change: {summary['single_most_valuable_change']}"
        )
        lines.append("")
    if summary["verdict"]:
        lines.append(f"Verdict: {summary['verdict']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "result", type=Path, help="a results/<date>-<mission>-<runner>.json"
    )
    parser.add_argument(
        "--missions-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "evals" / "blind" / "missions",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)
    try:
        result = json.loads(args.result.read_text())
        rubric = load_rubric(str(result.get("mission")), args.missions_dir)
        summary = score(result, rubric)
    except (BlindEvalError, OSError, json.JSONDecodeError) as exc:
        print(f"blind_eval_score: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
