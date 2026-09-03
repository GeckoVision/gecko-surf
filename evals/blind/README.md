# Blind evals

The product is judged by a stranger, on a schedule, against a fixed rubric, and the
score is a number a person can act on: `8/10, the two failures are the work`.

## The parts

| Part | Where | Who runs it |
|---|---|---|
| Mission brief + rubric | `missions/<mission>.md` and `missions/<mission>.rubric.json` | written once, changed by PR |
| The cold run | `blind-tester` agent (`.claude/agents/blind-tester.md`) | dispatched per mission by the main session |
| The deterministic suites + aggregation | `qa-agent` (`.claude/agents/qa-agent.md`) | after every deploy |
| The score and the checklist | `scripts/blind_eval_score.py`, run by the `coordinator` | after the QA report |
| Results | `results/<date>-<mission>-<runner>.json`, `results/<date>-qa-report.md` | committed; control plane only |

## The loop

1. Deploy.
2. `qa-agent` runs the deterministic suites and asks for the blind missions.
3. `blind-tester` runs each mission cold and writes the result JSON.
4. `coordinator` runs `uv run python scripts/blind_eval_score.py results/<date>-<mission>-<runner>.json`
   and gets the checklist: score, the failed checks with the lane that owns each,
   the friction lines. It routes each failure as work; one PR per fix.
5. Fix, deploy, repeat. The same rubric each time, so the delta is the measurement.

## Rubric rules

- A check is a yes/no question a cold agent can answer with evidence it fetched.
- Every check names the lane that owns a failure.
- `not_reached` is not a failure and not a pass; it is a hole in the run, reported.
- Change a rubric by PR, with the reason. A check that has passed three rounds in a
  row may be retired; a check nobody can pass is a product bug, not a rubric bug.

## Why the rubric is fixed

Three runs of the same brief on 2026-09-01 and 2026-09-02 gave three different
request counts and three different friction lists, and the only comparable numbers
were the ones asked the same way each time. The rubric is the same way.
