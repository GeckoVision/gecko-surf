"""Run a lifecycle through the state-advancing fork preflight — read-only, $0.

    uv run python scripts/fork_preflight.py --lifecycle <file.json> \
        --rpc-url http://127.0.0.1:8899 --network fork

TRANSPORT ONLY. Everything this file does is: parse argv, read a lifecycle description,
call :func:`gecko.fork_preflight.run_preflight`, print the outcome, and return the
report's exit code. All comprehension lives in the package.

THE EXIT CODE IS THE VERDICT. Unlike the probe this replaces, a non-passing step exits
non-zero. Do not add a branch that swallows it.

Nothing is signed, nothing is sent, nothing is written to disk. State is advanced by
LOCAL fork account overrides (surfpool's ``surfnet_setAccount`` cheatcode), which edits
the local validator's state and never touches a chain. Everything the RPC and the
builder return is untrusted third-party data: it is printed inside a capped, labelled
block and never acted on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.fork_preflight import (  # noqa: E402
    PreflightReport,
    run_preflight,
    steps_from_dicts,
    surfnet_overlay,
)
from gecko.networks import NETWORKS, coerce_network  # noqa: E402

#: How much untrusted text we are willing to print per line.
LINE_CAP = 160


def render(report: PreflightReport) -> str:
    """One block per step: status, CU, and the exact error string when it did not pass."""
    lines = [
        f"network        {report.network} (as stated, never inferred)",
        f"label          {report.network_label}",
        "",
        f"{'STEP':<22} {'STATUS':<14} {'CU':>10}  DETAIL",
        "-" * 100,
    ]
    for step in report.steps:
        units = f"{step.units_consumed:,}" if step.units_consumed else "-"
        detail = ""
        if step.refusal:
            detail = f"REFUSED: {step.refusal}"
        elif step.receipt is not None and step.status != "pass":
            detail = (
                f"revert_class={step.receipt.revert_class} err={step.receipt.err!r}"
            )
        elif step.carried:
            detail = f"carried forward: {', '.join(step.carried)}"
        lines.append(
            f"{step.name:<22} {step.status.upper():<14} {units:>10}  {detail[:LINE_CAP]}"
        )
        if (
            step.receipt is not None
            and step.receipt.logs_tail
            and step.status != "pass"
        ):
            lines.append("    ```untrusted-rpc-output (program logs, tail)")
            for log in step.receipt.logs_tail[-6:]:
                lines.append(f"    {log[:LINE_CAP]}")
            lines.append("    ```")
    lines.append("")
    lines.append(
        f"VERDICT: {'PASS' if report.ok else 'FAIL'} (exit {report.exit_code})"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lifecycle",
        required=True,
        help="JSON file: {'steps': [...]} — see the module.",
    )
    parser.add_argument("--rpc-url", required=True, help="The fork RPC (read-only).")
    parser.add_argument(
        "--network",
        required=True,
        choices=sorted(NETWORKS),
        help="Which network --rpc-url points at. You say; nothing here infers it.",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help=(
            "Withhold the override seam, reproducing the pre-fix behaviour: every step "
            "re-runs against the same pre-state. The runner REFUSES rather than doing it."
        ),
    )
    args = parser.parse_args(argv)

    document = json.loads(Path(args.lifecycle).read_text(encoding="utf-8"))
    steps = steps_from_dicts(document["steps"])
    overlay = None if args.no_overlay else surfnet_overlay(args.rpc_url)

    report = run_preflight(
        steps,
        rpc_url=args.rpc_url,
        network=coerce_network(args.network),
        overlay_apply=overlay,
    )
    print(render(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
