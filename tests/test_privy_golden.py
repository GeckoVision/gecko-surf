"""Privy golden-set invariants — the first real >50-op labeled ground truth.

Privy is kept OUT of the frozen small-set parametrization (test_golden_set.py) on purpose:
its point is SCALE (159 usable ops crosses the 50-op gate), not the zero-overlap 0/97 path
those tiny sets exercise. These checks keep the labels honest and frozen: every expect_op
names a real surfaced tool under the labeling session, the pool is the ~159 the scale gate
needs, the archetype mix is present, and the intents cannot be silently edited (sha256).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from gecko.access import Session
from gecko.client import AgentApiClient
from gecko.evaluate import GOLDEN_ARCHETYPES, load_golden

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"
SPEC = GOLDEN / "privy_openapi.json"


def _client() -> AgentApiClient:
    # Two-token dummy session so every auth-gated op surfaces (Privy is fully auth-gated).
    return AgentApiClient(
        str(SPEC), session=Session(jwt="recorded", api_token="recorded")
    )


def _tasks() -> list:
    return load_golden(GOLDEN / "privy_tasks.jsonl")


def test_privy_comprehends_past_the_scale_gate() -> None:
    assert len(_client().list_tools()) == 159, "privy must cross the >50-op scale gate"


def test_expect_ops_are_real_surfaced_tools() -> None:
    surfaced = {t["name"] for t in _client().list_tools()}
    for t in _tasks():
        for op in t.expect_ops:
            assert op in surfaced, (
                f"expect_op {op!r} not surfaced under labeling session"
            )


def test_archetype_mix_present() -> None:
    present = {t.archetype for t in _tasks()}
    assert present == GOLDEN_ARCHETYPES, (
        f"missing archetypes {GOLDEN_ARCHETYPES - present}"
    )


def test_frozen_hash_matches() -> None:
    path = GOLDEN / "privy_tasks.jsonl"
    committed = (GOLDEN / "privy_tasks.jsonl.sha256").read_text().strip()
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == committed, (
        "privy golden set changed but its .sha256 was not re-frozen"
    )
