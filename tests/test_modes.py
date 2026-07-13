"""Task 1.1 — the canonical ``CallMode`` + the probe→synthetic corpus routing.

``gecko.modes`` is the single source of truth for the mode type (rules:
shared Literals live in ONE module; every consumer imports, never redeclares).
The control-plane gate: a probe outcome must land in ``synthetic.jsonl``,
never the main corpus (the corpus stays retired for probe traffic).
"""

from __future__ import annotations

import json
from typing import get_args

from gecko import corpus
from gecko.modes import CALL_MODES, CallMode, coerce_mode


def test_callmode_is_the_canonical_literal() -> None:
    assert set(get_args(CallMode)) == {"recorded", "live", "probe"}
    assert CALL_MODES == frozenset({"recorded", "live", "probe"})


def test_probe_source_is_synthetic() -> None:
    assert corpus.source_for_mode("probe") == "synthetic"


def test_coerce_mode_accepts_canonical_values_and_fails_closed() -> None:
    assert coerce_mode("recorded") == "recorded"
    assert coerce_mode("live") == "live"
    assert coerce_mode("probe") == "probe"
    # Unknown / absent -> the $0 offline default, matching the engine's existing
    # "anything not live synthesizes" behavior (fail closed, never fire live).
    assert coerce_mode("prod") == "recorded"
    assert coerce_mode(None) == "recorded"
    assert coerce_mode("junk", default="probe") == "probe"


def test_probe_outcome_routes_to_synthetic_jsonl_never_the_main_corpus(
    tmp_path,
) -> None:
    outcome = corpus.outcome_from(
        operation_id="getBalance",
        tool_invoke={"method": "GET", "path": "/balance/{id}"},
        args={"id": 7},
        status=422,
        error_class="unprocessable_422",
        latency_ms=None,
        mode="probe",
        auth_injected=False,
        ts=0,
        surface_id="s",
        surface_rev="r",
    )
    assert outcome.source == "synthetic"

    main = tmp_path / "corpus.jsonl"
    corpus.record(outcome, main)

    assert not main.exists(), "a probe outcome must never reach the main corpus"
    sibling = corpus.synthetic_sibling(main)
    assert sibling.exists()
    row = json.loads(sibling.read_text(encoding="utf-8").strip())
    assert row["mode"] == "probe"
    assert row["source"] == "synthetic"
