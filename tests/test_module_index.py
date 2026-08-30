"""The generated index must describe what a READER OF THE REPO can open.

This exists because the failure already reached main. `gecko/vocab_gap.py` is held back
deliberately and sits untracked in a working tree; the index was generated from that tree
and committed, so main documented a module that is not there. A reader who trusted it
would go looking for a file they cannot open — in the one file whose entire job is saying
what exists.

Regenerating from an in-progress checkout is the NORMAL case, not a mistake. So the
generator has to be what refuses, rather than every human remembering to check
`git status` first.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import module_index  # noqa: E402


def _tracked_modules() -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "gecko/*.py", "gecko/**/*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        line
        for line in out.stdout.splitlines()
        if line.strip() and "__init__" not in line
    }


def test_an_untracked_module_is_not_indexed(tmp_path) -> None:
    """The regression, driven directly: drop a .py into gecko/ without adding it, and it
    must NOT appear. Named `zz_` so it sorts last and cannot mask a real row."""
    ghost = ROOT / "gecko" / "zz_untracked_probe.py"
    assert not ghost.exists(), "probe name collides with a real module"
    ghost.write_text('"""A module that exists only in this working tree."""\n')
    try:
        rendered = module_index.build()
    finally:
        ghost.unlink()
    assert "zz_untracked_probe" not in rendered, (
        "the index documented a module that is not tracked — a reader cannot open it"
    )


def test_every_indexed_module_is_tracked() -> None:
    """The general form: nothing in the committed index may be untracked."""
    indexed = {
        line.split("`")[1]
        for line in (ROOT / "docs" / "module-index.md").read_text().splitlines()
        if line.startswith("| `")
    }
    tracked = {
        p[len("gecko/") :].removesuffix(".py").replace("/", ".")
        for p in _tracked_modules()
    }
    missing = indexed - tracked
    assert not missing, (
        f"docs/module-index.md documents modules that are not tracked: {sorted(missing)}"
    )


def test_the_count_in_the_header_matches_the_rows() -> None:
    """A header that disagrees with its own table is how a stale index reads as fresh."""
    text = (ROOT / "docs" / "module-index.md").read_text()
    header = next(line for line in text.splitlines() if "modules in" in line)
    declared = int(header.split()[0])
    rows = sum(1 for line in text.splitlines() if line.startswith("| `"))
    assert declared == rows, f"header says {declared} modules, table has {rows} rows"
