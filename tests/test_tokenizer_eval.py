"""The camelCase-tokenizer falsifier as a CI guard.

`scripts/tokenizer_eval.py` compares arm A (baseline tokenizer) vs arm B (+camelCase
split) with `evaluate_golden` on the committed golden sets. The load-bearing claim is a
SUPERSET one: the split can only add recall, never remove a match — so arm B must never
regress below arm A on any set at any k. (Whether it also RISES is scale/thin-summary
bound; the mechanism itself is proven by test_catalog::test_camelcase_operation_id_*.)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "tokenizer_eval",
    Path(__file__).resolve().parent.parent / "scripts" / "tokenizer_eval.py",
)
assert _SPEC and _SPEC.loader
tokenizer_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tokenizer_eval)


def test_camelcase_arm_never_regresses_on_golden_sets() -> None:
    results = tokenizer_eval.run()
    no_regression, _rose = tokenizer_eval.decide(results)
    assert no_regression, (
        "camelCase tokenizer must be a strict superset — no recall regression"
    )


def test_eval_covers_both_committed_golden_sets() -> None:
    results = tokenizer_eval.run()
    assert set(results) == {"txodds", "pegana"}


def test_eval_restores_the_shipped_tokenizer() -> None:
    from gecko import catalog

    before = catalog._tokens
    tokenizer_eval.run()
    assert catalog._tokens is before, "harness must not leave the module global patched"
