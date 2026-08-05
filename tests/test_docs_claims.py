"""Docs cannot rot silently (docs/engineering-practices.md §4).

Mechanical truth checks on the living docs: every repo-relative link the README and
``architecture.llms.txt`` reference must exist, and every ``gecko/*.py`` module path
named in the docs must be a real file. CI fails on a dead link or a renamed module —
prose stays human-maintained, existence is machine-checked.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DOCS = ["README.md", "architecture.llms.txt", "docs/architecture.md"]

# Markdown links to repo-relative targets: [text](path) — skip http(s), anchors, mail.
_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)\s]*)?\)")

# Module paths named in prose/tables, e.g. gecko/simulate.py or gecko/providers/cli.py
_MODULE = re.compile(r"\bgecko/[A-Za-z0-9_/]+\.py\b")


def _targets(doc: Path) -> list[tuple[str, Path]]:
    text = doc.read_text(encoding="utf-8")
    out: list[tuple[str, Path]] = []
    for match in _LINK.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        out.append((target, (doc.parent / target).resolve()))
    return out


def test_repo_relative_links_resolve() -> None:
    missing: list[str] = []
    for name in DOCS:
        doc = REPO / name
        assert doc.exists(), f"living doc {name} is itself missing"
        for target, resolved in _targets(doc):
            if not resolved.exists():
                missing.append(f"{name} -> {target}")
    assert not missing, f"dead repo-relative links: {missing}"


def test_module_paths_named_in_docs_exist() -> None:
    missing: list[str] = []
    for name in DOCS:
        doc = REPO / name
        if not doc.exists():
            continue
        for module in set(_MODULE.findall(doc.read_text(encoding="utf-8"))):
            if not (REPO / module).exists():
                missing.append(f"{name} -> {module}")
    assert not missing, f"docs name modules that do not exist: {missing}"
