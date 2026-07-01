"""CLI tests — ``gecko-redteam`` is thin transport over the harness + scorer + report.

The gate is the exit code: 0 iff ``money_trusted``. Run through ``python -m gecko.redteam``
so we exercise the real entrypoint (not just ``_run``), proving the console-script wiring.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

_ALLOWED_KEYS = {
    "ts",
    "scenario_id",
    "family",
    "tier",
    "layer",
    "vector",
    "polarity",
    "policy_id",
    "defenses",
    "verdict",
    "tripped_predicate",
    "blocked_reason",
    "leaked",
    "leak_sink",
    "auth_host_ok",
}


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "gecko.redteam", *args],
        cwd=_REPO,
        env={"PYTHONPATH": str(_REPO), "PATH": ""},
        capture_output=True,
        text=True,
    )


def test_cli_builtin_defended_exit_zero():
    result = _cli("builtin", "--defenses", "all")
    assert result.returncode == 0, result.stderr
    assert "money_trusted=True" in result.stdout


def test_cli_builtin_naive_exit_one():
    result = _cli("builtin", "--defenses", "none")
    assert result.returncode == 1, result.stdout
    assert "money_trusted=False" in result.stdout


def test_cli_default_spec_and_defenses_is_the_defended_gate():
    # no positional / no flags == builtin + defenses=all == the CI green path
    result = _cli()
    assert result.returncode == 0, result.stderr


def test_cli_json_output_parses():
    result = _cli("builtin", "--defenses", "all", "--json")
    assert result.returncode == 0, result.stderr
    blob = json.loads(result.stdout)
    assert blob["money_trusted"] is True
    assert blob["tier0_asr"] == 0.0


def test_cli_audit_jsonl_is_allowlisted(tmp_path):
    audit = tmp_path / "run.jsonl"
    result = _cli("builtin", "--defenses", "all", "--audit", str(audit))
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 12
    for line in lines:
        assert set(json.loads(line)) == _ALLOWED_KEYS


def test_cli_unknown_spec_errors_cleanly():
    result = _cli("some-other-spec.json")
    assert result.returncode != 0
    assert "builtin" in (result.stderr + result.stdout).lower()


def test_cli_llm_policy_errors_cleanly_when_extra_absent():
    # the non-CI LLM lane imports lazily; absent the extra it must fail with a clear
    # message and a non-zero code, never a traceback the user can't act on.
    result = _cli("builtin", "--policy", "llm")
    assert result.returncode != 0
    assert "llm" in (result.stderr + result.stdout).lower()
