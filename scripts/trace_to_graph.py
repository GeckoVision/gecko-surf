"""Draw the graph of a run from its trace: JSONL in, an archify sequence spec out.

    uv run python scripts/trace_to_graph.py run.jsonl --out run.workflow.json [--html run.html]

The spec is the deliverable; the HTML is archify's rendering of it (``--html`` shells out
to the archify CLI when it is installed, and says so when it is not). Nothing here reads
the chain or the repo: the trace is the only input, so two runs that took the same steps
produce the same drawing, and a run that refused produces a drawing with the refusal on it.

Participants are the parties. Messages are the steps, in order, each sent by the run to
the party that took it. A refusal is a security message carrying its code, and the
sequence stops there. That is the point: the graph
of a refused run looks different from the graph of a landed one, and both are generated,
never drawn.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.trace import Party, Trace, TraceRow  # noqa: E402

#: Party -> (label, archify component type).
#: Labels stay short: archify's participant box is 86px wide; detail goes in the sublabel.
PARTIES: dict[str, tuple[str, str, str]] = {
    "gecko": ("Gecko", "verifies", "backend"),
    "builder": ("Builder", "assembles", "cloud"),
    "relay": ("Relay", "pays the fee", "cloud"),
    "buyer": ("Buyer", "authorises", "security"),
    "node": ("Node", "simulates, lands", "external"),
    "store": ("Store", "is paid", "database"),
}
#: The driver of the sequence: the lane itself, which asks each party in turn.
RUN = "run"
FIRST_Y = 185
STEP_Y = 46

#: Where the archify skill lives when installed for the user. Overridable by env.
ARCHIFY = Path(
    os.environ.get(
        "ARCHIFY_CLI", str(Path.home() / ".claude/skills/archify/bin/archify.mjs")
    )
)


def _label(row: TraceRow) -> str:
    parts = [row.step, f"{row.ms} ms"]
    if row.units is not None:
        parts.append(f"{row.units:,} CU")
    if row.binding_prefix:
        parts.append(row.binding_prefix)
    return " · ".join(parts)


def spec_from_trace(trace: Trace) -> dict[str, Any]:
    """The archify ``sequence`` spec for one trace. Pure; no I/O.

    The lane is the driver: every step is a message from the run to the party that took
    it. A refusal is a security-variant message carrying its code, and the sequence stops
    there, so a refused run and a landed run are different drawings of the same shape.
    """
    parties: list[Party] = []
    for row in trace.rows:
        if row.party not in parties:
            parties.append(row.party)
    participants: list[dict[str, Any]] = [
        {
            "id": RUN,
            "type": "backend",
            "label": trace.lane,
            "sublabel": f"on {trace.network}",
        }
    ]
    participants.extend(
        {
            "id": party,
            "type": PARTIES[party][2],
            "label": PARTIES[party][0],
            "sublabel": PARTIES[party][1],
        }
        for party in parties
    )

    messages: list[dict[str, Any]] = []
    y = FIRST_Y
    for row in trace.rows:
        ok = row.outcome == "ok"
        message: dict[str, Any] = {
            "id": f"s{row.seq}_{row.step}".replace("-", "_"),
            "from": RUN,
            "to": row.party,
            "y": y,
            "label": _label(row),
            "variant": "security" if not ok else "default",
        }
        if not ok:
            message["note"] = f"refused: {row.outcome}"
        elif row.note:
            message["note"] = row.note
        messages.append(message)
        y += STEP_Y
        if not ok:
            break

    refused = trace.refused
    landed = refused is None and bool(trace.rows)
    verdict = (
        "landed" if landed else (f"refused: {refused.outcome}" if refused else "empty")
    )
    total_ms = sum(row.ms for row in trace.rows)
    return {
        "schema_version": 1,
        "diagram_type": "sequence",
        "meta": {
            "title": f"{trace.lane} on {trace.network}: {verdict}",
            "subtitle": (
                f"{len(trace.rows)} steps, {total_ms} ms, drawn from the run's own trace"
            ),
            "quality_profile": "showcase",
            "viewBox": [760, max(480, y + 120)],
        },
        "participants": participants,
        "messages": messages,
    }


def render(spec_path: Path, html_path: Path) -> dict[str, Any]:
    """Ask archify to deliver the HTML. Returns its JSON receipt, or a reason it could not."""
    node = shutil.which("node")
    if node is None or not ARCHIFY.exists():
        return {"rendered": False, "reason": "archify CLI or node not installed"}
    completed = subprocess.run(
        [
            node,
            str(ARCHIFY),
            "deliver",
            "sequence",
            str(spec_path),
            str(html_path),
            "--quality",
            "showcase",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        receipt = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        receipt = {"raw": completed.stdout[-2000:]}
    receipt["rendered"] = completed.returncode == 0 and receipt.get("ok", True)
    if not receipt["rendered"]:
        receipt["reason"] = receipt.get("error") or completed.stderr[-2000:]
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("trace", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="workflow spec JSON")
    parser.add_argument("--html", type=Path, default=None, help="render with archify")
    args = parser.parse_args(argv)
    trace = Trace.read(args.trace)
    spec = spec_from_trace(trace)
    args.out.write_text(json.dumps(spec, indent=2) + "\n")
    print(
        f"spec     {args.out}  ({len(spec['participants'])} participants, "
        f"{len(spec['messages'])} messages)"
    )
    if args.html is not None:
        receipt = render(args.out, args.html)
        if receipt.get("rendered"):
            print(f"html     {args.html}")
        else:
            print(
                f"html     NOT rendered: {receipt.get('reason') or receipt.get('stderr')}"
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
