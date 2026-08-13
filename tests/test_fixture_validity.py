"""Every JSON fixture parses. The defect class, not the instance.

``tests/fixtures/petstore_openapi.json`` sat in the tree with a trailing comma for days —
invalid JSON that nothing noticed, because nothing loaded it: the suite it was written for
uses inline specs. A fixture nothing loads is a claim nothing checks, and the first test
that finally reaches for it fails on the fixture instead of on its subject.

This sweep makes validity itself the pinned property: every ``*.json`` under
``tests/fixtures`` must parse, and every ``*.jsonl`` must parse line by line. It does not
validate schemas — a fixture is allowed to be wrong-shaped on purpose (hostile-input
suites depend on that) — but a file whose bytes cannot even parse is not a hostile
fixture, it is a broken one, and the difference is that nothing can USE a broken one to
prove anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"

_JSON_FILES = sorted(_FIXTURES.glob("**/*.json"))
_JSONL_FILES = sorted(_FIXTURES.glob("**/*.jsonl"))


def test_the_sweep_sees_the_fixtures_at_all() -> None:
    """A glob that silently matches nothing would make every test below vacuous."""
    assert _JSON_FILES, "no *.json fixtures found — the sweep is aimed at nothing"


@pytest.mark.parametrize("path", _JSON_FILES, ids=lambda p: p.name)
def test_every_json_fixture_parses(path: Path) -> None:
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _JSONL_FILES, ids=lambda p: p.name)
def test_every_jsonl_fixture_parses_line_by_line(path: Path) -> None:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{path.name}:{number} is not valid JSON: {exc}")
