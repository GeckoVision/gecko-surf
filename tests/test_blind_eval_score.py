"""The blind-eval scorer: rubric join, the checklist text, and the errors that refuse to score."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.blind_eval_score import BlindEvalError, load_rubric, main, render, score  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MISSIONS = ROOT / "evals" / "blind" / "missions"
RESULTS = ROOT / "evals" / "blind" / "results"

RUBRIC = {
    "mission": "m",
    "version": 1,
    "checks": [
        {"id": "a", "question": "A?", "lane": "software-engineer"},
        {"id": "b", "question": "B?", "lane": "tech-docs-writer"},
        {"id": "c", "question": "C?", "lane": "product-engineer"},
    ],
}


def _result(**answers: str) -> dict:
    return {
        "mission": "m",
        "date": "2026-09-02",
        "runner": "blind-tester",
        "checks": [
            {"id": k, "result": v, "evidence": f"ev-{k}"} for k, v in answers.items()
        ],
        "friction": ["one thing"],
        "verdict": "yes",
        "single_most_valuable_change": "do x",
    }


def test_score_counts_passes_and_names_failures_with_their_lane() -> None:
    summary = score(_result(a="pass", b="fail", c="not_reached"), RUBRIC)
    assert (summary["passed"], summary["total"]) == (1, 3)
    assert [r["id"] for r in summary["failed"]] == ["b"]
    assert summary["failed"][0]["lane"] == "tech-docs-writer"
    assert [r["id"] for r in summary["not_reached"]] == ["c"]


def test_render_leads_with_the_score_and_lists_the_work() -> None:
    text = render(score(_result(a="pass", b="fail", c="pass"), RUBRIC))
    assert text.startswith("# m: 2/3")
    assert "- [ ] `b` (tech-docs-writer): B?" in text
    assert "evidence: ev-b" in text
    assert "- [x] `a`: ev-a" in text
    assert "one thing" in text and "do x" in text


@pytest.mark.parametrize(
    "answers, message",
    [
        ({"a": "pass", "b": "pass"}, "does not answer: c"),
        ({"a": "pass", "b": "pass", "c": "pass", "d": "pass"}, "rubric does not have"),
        ({"a": "maybe", "b": "pass", "c": "pass"}, "must be one of"),
    ],
)
def test_a_result_that_does_not_line_up_with_the_rubric_is_not_scored(
    answers: dict, message: str
) -> None:
    with pytest.raises(BlindEvalError, match=message):
        score(_result(**answers), RUBRIC)


def test_a_duplicate_answer_is_refused() -> None:
    result = _result(a="pass", b="pass", c="pass")
    result["checks"].append({"id": "a", "result": "fail", "evidence": ""})
    with pytest.raises(BlindEvalError, match="twice"):
        score(result, RUBRIC)


def test_the_committed_results_score_against_their_rubrics() -> None:
    # Every committed result must line up with its rubric: a rubric change that
    # orphans a result fails here, not in the coordinator's hands.
    files = sorted(RESULTS.glob("*.json"))
    assert files, "no committed results"
    for path in files:
        result = json.loads(path.read_text())
        summary = score(result, load_rubric(result["mission"], MISSIONS))
        assert summary["total"] == len(summary["rows"])


def test_the_first_committed_run_scores_eight_of_ten() -> None:
    path = RESULTS / "2026-09-02-geckocoffee-purchase-manual.json"
    result = json.loads(path.read_text())
    summary = score(result, load_rubric("geckocoffee-purchase", MISSIONS))
    assert (summary["passed"], summary["total"]) == (8, 10)
    assert {r["id"] for r in summary["failed"]} == {
        "no-html-detour",
        "headless-path-in-docs",
    }


def test_cli_prints_the_checklist_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main([str(RESULTS / "2026-09-02-geckocoffee-purchase-manual.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("# geckocoffee-purchase: 8/10")


def test_cli_refuses_a_result_with_no_rubric(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "x.json"
    bad.write_text(json.dumps({"mission": "no-such-mission", "checks": []}))
    assert main([str(bad)]) == 2
    assert "no rubric" in capsys.readouterr().err
