"""The provider report, end to end: point it at ONE surface, get the document.

    uv run --extra fcc python -m scripts.provider_report --surface pegana
    uv run --extra fcc python -m scripts.provider_report --surface pegana --n 5 --out report.md

WHAT IT DOES. Loads a surface and its golden intents, runs `gecko.fcc_eval` over both arms —
the raw specification an agent gets today, and the comprehended surface — and renders
`gecko.score`'s provider report. The document is the deliverable: what an agent gets right
before, what it gets right after, what broke, what still fails, and which gate moved.

WHY IT IS ONE SURFACE PER RUN, and it is not a limitation. `fcc_eval.lift`'s own docstring
records the reason: a pooled +0.30 was once the mean of a +0.55 API and a +0.00 API, and
described neither. `gecko.score.score_surface` raises on mixed records rather than averaging,
so this script iterates surfaces and writes one document each.

WHAT IT COSTS. One cheap-model call per (intent, arm, run) — that is the only spend. The API
itself is never called: the arms are tool DEFINITIONS handed to a model, and the pick is
scored against the golden set. Nothing is signed, sent, or charged to the provider.

WHERE IT WRITES. `private/` by default (gitignored). A report names a provider's weaknesses,
so it is a document you choose to send, never one that lands in a public tree by default.
`--out` overrides.

Reads CLAUDE_API_KEY from the environment or a repo `.env`. Never printed, never logged.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gecko.access import AuthSession, Session, public_session  # noqa: E402
from gecko.client import AgentApiClient  # noqa: E402
from gecko.enrich import BLURB_MODEL  # noqa: E402
from gecko.evaluate import load_golden  # noqa: E402
from gecko.fcc_eval import RunRecord, evaluate_fcc  # noqa: E402
from gecko.score import ScoreError, SurfaceScore, render_report, score_surface  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "golden"

#: The surfaces this repo can score today. A provider surface is a spec plus the session that
#: reads it plus a golden set of intents in USER words — and the third is the part that cannot
#: be generated, which is why the list is short and honest about why.
#: `AuthSession` is the Protocol both concrete sessions satisfy — a two-token `Session` and
#: the `NoAuthSession` a public read uses. Typing the registry to the protocol rather than to
#: one implementation is the point of the adapter seam: a surface that needs a different kind
#: of session should not need a change here.
SURFACES: Mapping[str, tuple[Path, Callable[[], AuthSession], str]] = {
    "txodds": (
        ROOT / "tests" / "fixtures" / "txodds_docs.yaml",
        lambda: Session(jwt="recorded-mode", api_token="recorded-mode"),
        "18-op regression-guard proxy",
    ),
    "pegana": (
        ROOT / "tests" / "fixtures" / "pegana_openapi.json",
        public_session,
        "41-op spec, 26 usable under public read",
    ),
}

#: The four gates, in the words a provider uses about their own surface rather than ours.
#: "args_match" is our vocabulary; "the arguments were right" is theirs, and the report is
#: for them.
GATE_NAMES: Mapping[str, str] = {
    "retrieval": "the agent could FIND the call",
    "tool_correct": "it picked the right call",
    "well_formed": "the call was shaped correctly",
    "args_match": "the arguments were right",
}


class ReportError(RuntimeError):
    """The report could not be produced. Never a partial document."""


def read_key(name: str = "CLAUDE_API_KEY") -> str:
    """The model key, from the environment or a repo `.env`. Returned, never logged."""
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
    except Exception:  # noqa: BLE001 — .env discovery is best-effort
        pass
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(rf"^{re.escape(name)}=(.*)$", line.strip())
            if match:
                return match.group(1).strip().strip('"').strip("'")
    raise ReportError(
        f"{name} not found in the environment or a repo .env. The report needs one cheap "
        "model call per intent per arm per run — there is no offline path, because the "
        "thing being measured is whether a model gets the call right."
    )


def measure(
    surface: str, *, n_runs: int, k: int, llm: object, model: str
) -> list[RunRecord]:
    """Run both arms over one surface's golden intents."""
    if surface not in SURFACES:
        raise ReportError(
            f"unknown surface {surface!r}. Known: {', '.join(sorted(SURFACES))}. A surface "
            "needs a spec, a session, and a golden set of intents in user words."
        )
    spec, session_factory, _ = SURFACES[surface]
    tasks_path = GOLDEN / f"{surface}_tasks.jsonl"
    if not spec.exists():
        raise ReportError(f"{spec} is missing — nothing to comprehend")
    if not tasks_path.exists():
        raise ReportError(
            f"{tasks_path} is missing. The golden set is the intents in a USER's words, and "
            "it is the one input that cannot be generated from the spec — a report scored "
            "against machine-generated phrasings would measure keyword echo, not reach."
        )

    client = AgentApiClient(str(spec), session=session_factory())
    tasks = load_golden(tasks_path)
    return evaluate_fcc(
        surface,
        client,
        tasks,
        llm,  # type: ignore[arg-type]
        model=model,
        k=k,
        n_runs=n_runs,
    )


def report_for(records: list[RunRecord], *, surface: str) -> tuple[SurfaceScore, str]:
    try:
        score = score_surface(records, surface=surface)
    except ScoreError as exc:  # a refusal from the scorer is a refusal here
        raise ReportError(str(exc)) from exc
    return score, render_report(score, gate_names=GATE_NAMES)


def _summary_line(score: SurfaceScore) -> str:
    if score.lift_is_noise:
        return (
            f"{score.surface}: undetermined — the change is inside this run's own "
            f"variance (±{score.noise_floor * 100:.0f} pts). Run more."
        )
    return (
        f"{score.surface}: {score.before.fcc * 100:.0f}% → {score.after.fcc * 100:.0f}% "
        f"({score.lift * 100:+.0f} pts), {len(score.fixed)} fixed, "
        f"{len(score.broke)} broken, {len(score.still_failing)} still failing"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument(
        "--surface",
        action="append",
        help=f"one of {', '.join(sorted(SURFACES))}. Repeatable; default is all of them.",
    )
    parser.add_argument("--n", type=int, default=3, help="runs per intent per arm")
    parser.add_argument(
        "--k", type=int, default=8, help="how many tools Gecko surfaces"
    )
    parser.add_argument(
        "--out", type=Path, help="write the document here (single surface)"
    )
    parser.add_argument(
        "--records",
        type=Path,
        help="also write the raw run records as JSONL (shapes and booleans, no arg values)",
    )
    args = parser.parse_args()

    wanted = args.surface or sorted(SURFACES)
    if args.out and len(wanted) > 1:
        raise ReportError(
            "--out names one file and more than one surface was requested. A report "
            "describes ONE surface; run it once per surface."
        )

    import anthropic  # imported here so --help works without the extra

    llm = anthropic.Anthropic(api_key=read_key())

    summaries: list[str] = []
    for surface in wanted:
        records = measure(surface, n_runs=args.n, k=args.k, llm=llm, model=BLURB_MODEL)
        score, document = report_for(records, surface=surface)

        destination = args.out or (ROOT / "private" / f"provider-report-{surface}.md")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(document, encoding="utf-8")

        if args.records:
            args.records.parent.mkdir(parents=True, exist_ok=True)
            with args.records.open("w", encoding="utf-8") as handle:
                for record in records:  # shapes + booleans only, never arg VALUES
                    handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        print(document)
        print(f"\nwrote {destination}")
        summaries.append(_summary_line(score))

    if len(summaries) > 1:
        # Each surface printed its own document above. This is a list, deliberately NOT a
        # pooled figure: averaging surfaces of unlike difficulty describes none of them.
        print("\n" + "\n".join(f"  {line}" for line in summaries))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReportError as exc:
        raise SystemExit(f"provider report refused: {exc}") from exc
