"""Golden-set invariants — the retrieval eval substrate is frozen, honest, and hashed.

These tests make the golden set falsifiable BEFORE any semantic stage is measured against
it (plan §1): they mechanically prove the zero-overlap paraphrases exercise the `score > 0`
empty-drop (the 0/97 path), that every label names a real surfaced tool under its labeling
session, that the required archetype mix is present, and — critically — that the frozen
intents cannot be silently edited after seeing results (sha256 freeze).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

import pytest

from gecko.access import Session, public_session
from gecko.catalog import CatalogEntry, _tokens
from gecko.client import AgentApiClient
from gecko.evaluate import GOLDEN_ARCHETYPES, GoldenTask, load_golden

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden"


# Each golden file is LABELED and SCORED under one session — the same session, so an
# auth-gated op hidden by `_usable_tool_names` never reads as a retrieval miss (scope
# artifact). TxODDS uses a dummy two-token session so the paid ops surface; Pegana is a
# public read surface.
def _txodds_session() -> Session:
    return Session(jwt="recorded-mode", api_token="recorded-mode")


CASES: dict[str, tuple[str, Callable[[], object]]] = {
    "txodds": ("txodds_docs.yaml", _txodds_session),
    "pegana": ("pegana_openapi.json", public_session),
}


def _client(spec_name: str, session_factory: Callable[[], object]) -> AgentApiClient:
    return AgentApiClient(str(FIXTURES / spec_name), session=session_factory())


def _tasks(name: str) -> list[GoldenTask]:
    return load_golden(GOLDEN / f"{name}_tasks.jsonl")


@pytest.mark.parametrize("name", list(CASES))
def test_golden_file_parses(name: str) -> None:
    tasks = _tasks(name)
    assert tasks, "golden file must contain tasks"
    for t in tasks:
        assert t.archetype in GOLDEN_ARCHETYPES
        if t.archetype == "out_of_scope":
            assert t.expect_ops == (), "out_of_scope tasks must have empty expect_ops"
        else:
            assert t.expect_ops, "in-scope tasks must name >=1 expected op"


@pytest.mark.parametrize("name", list(CASES))
def test_expect_ops_are_real_surfaced_tools(name: str) -> None:
    """Labels reference REAL tool names (post `_safe_name`) that surface under the labeling
    session — so sanitization can never silently mismatch and inflate 'misses'."""
    spec_name, session_factory = CASES[name]
    client = _client(spec_name, session_factory)
    surfaced = {t["name"] for t in client.list_tools()}
    for t in _tasks(name):
        for op in t.expect_ops:
            assert op in surfaced, (
                f"{name}: expect_op {op!r} not surfaced under labeling session"
            )


@pytest.mark.parametrize("name", list(CASES))
def test_zero_overlap_invariant(name: str) -> None:
    """For every paraphrase_no_overlap task, the goal shares NO token with the expected
    op's haystack — mechanically guaranteeing the `score > 0` empty-drop (0/97) is
    exercised, not asserted by vibes."""
    spec_name, session_factory = CASES[name]
    client = _client(spec_name, session_factory)
    by_name = {CatalogEntry(o).tool_name: CatalogEntry(o) for o in client.operations}
    checked = 0
    for t in _tasks(name):
        if t.archetype != "paraphrase_no_overlap":
            continue
        goal_tokens = _tokens(t.goal)
        for op in t.expect_ops:
            overlap = goal_tokens & _tokens(by_name[op]._haystack)
            assert not overlap, (
                f"{name}: {t.goal!r} overlaps {op} haystack on {overlap}"
            )
            checked += 1
    assert checked, f"{name}: no paraphrase_no_overlap tasks to check the invariant"


@pytest.mark.parametrize("name", list(CASES))
def test_required_archetype_mix_present(name: str) -> None:
    present = {t.archetype for t in _tasks(name)}
    assert present == GOLDEN_ARCHETYPES, (
        f"{name}: missing archetypes {GOLDEN_ARCHETYPES - present}"
    )


@pytest.mark.parametrize("name", list(CASES))
def test_frozen_hash_matches(name: str) -> None:
    """The intents are FROZEN: the committed sha256 sidecar must match the file's current
    hash, so nobody edits a goal after seeing a blurb/dense result (author-coupling guard)."""
    path = GOLDEN / f"{name}_tasks.jsonl"
    committed = (GOLDEN / f"{name}_tasks.jsonl.sha256").read_text().strip()
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == committed, (
        f"{name} golden set changed but its .sha256 was not re-frozen "
        f"(expected {committed}, got {actual}) — re-freeze only via a reviewed step"
    )
