"""gecko-redteam — run the off-chain battle-test suite and gate on the scorecard.

Thin transport only: parse args, pick the paired policy + defense arm, run the packaged 12
scenarios through the harness, print the scorecard (or JSON), optionally write the
control-plane-safe decision audit, and exit non-zero unless ``money_trusted``. Every bit of
logic lives in the package (``scenarios`` / ``harness`` / ``scorer`` / ``report``).

Two lanes:
  * ``--policy scripted`` (default) — the deterministic $0 CI gate. ``--defenses all`` pairs
    the defended ScriptedPolicy with the ``DefendedArm``; ``--defenses none`` pairs the naive
    ScriptedPolicy with the ``NaiveArm``. This proves the SCORER + the arm's enforcement.
  * ``--policy llm`` — the non-CI lane (real agent robustness). Imports lazily so the engine
    stays dep-light; absent the extra it fails with a clear message (Pattern B).
"""

from __future__ import annotations

import argparse
import sys

from .harness import Arm, DefendedArm, NaiveArm, Policy, canary_session, run_suite
from .policies import defended_policy, naive_policy
from .report import render, render_json, write_audit
from .scenarios import SCENARIOS
from .scorer import score


def _load_llm_policy() -> Policy:
    """Lazily import the non-CI LLM policy; fail closed with an actionable message."""
    try:
        from .llm import LLMPolicy  # noqa: PLC0415 - lazy: keep the engine dep-light
    except ImportError:
        raise SystemExit(
            "gecko-redteam: --policy llm needs the optional LLM lane "
            "(gecko.redteam.llm); install the extra to run the non-CI robustness lane."
        ) from None
    return LLMPolicy()


def _build(policy_kind: str, defenses: str) -> tuple[Policy, Arm]:
    """Pick the (policy, arm) pair for the chosen lane. ``--defenses`` toggles the arm; the
    scripted policy is paired to it so CI has a deterministic pass/fail."""
    session = canary_session()
    if defenses == "all":
        arm: Arm = DefendedArm(session)
        scripted: Policy = defended_policy(SCENARIOS)
    else:
        arm = NaiveArm(session)
        scripted = naive_policy(SCENARIOS)
    policy = _load_llm_policy() if policy_kind == "llm" else scripted
    return policy, arm


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gecko-redteam",
        description="Run the off-chain battle-test suite; exit 0 iff money_trusted.",
    )
    parser.add_argument(
        "spec",
        nargs="?",
        default="builtin",
        help="'builtin' runs the 12 packaged scenarios (the CI default). Per-spec "
        "adversarial generation is a later lane.",
    )
    parser.add_argument("--policy", choices=("scripted", "llm"), default="scripted")
    parser.add_argument("--defenses", choices=("all", "none"), default="all")
    parser.add_argument(
        "--audit", default=None, help="write the decision audit JSONL here"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the scorecard as JSON"
    )
    args = parser.parse_args(argv)

    if args.spec != "builtin":
        parser.error(
            "v1 runs only the packaged 'builtin' suite; per-spec adversarial "
            "generation is a later lane."
        )

    policy, arm = _build(args.policy, args.defenses)
    # now=0 keeps the run deterministic (the corpus ts is a fixed control-plane field).
    outcomes = run_suite(SCENARIOS, policy, arm, now=0)
    card = score(outcomes)

    title = f"gecko-redteam · policy={args.policy} defenses={args.defenses}"
    print(render_json(card) if args.json else render(card, title=title))

    if args.audit:
        write_audit(outcomes, args.audit)
        print(f"wrote decision audit -> {args.audit}", file=sys.stderr)

    return 0 if card.money_trusted else 1


if __name__ == "__main__":
    raise SystemExit(_run())
