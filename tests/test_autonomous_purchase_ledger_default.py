"""The CLI's spend ledger must survive a restart unless someone asks for otherwise.

A rolling cap kept in memory is not a cap: restart the process and the day's budget is
new. On a laptop that is a footnote. Deployed on ECS it is a hole in the exact control the
spend gate exists to be — a rolling deploy replaces the task, and the daily cap resets
with it.

So the polarity matters more than the storage. The DURABLE ledger is what you get by
saying nothing; the ephemeral one has to be asked for by name. These tests pin that
direction, because a default that forgets is the kind of thing that gets re-introduced by
someone wiring a quick test and never changing it back.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from gecko.spend_policy import FileSpendLedger, InMemorySpendLedger

_REPO = Path(__file__).resolve().parents[1]


def _cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "autonomous_purchase_cli", _REPO / "scripts" / "autonomous_purchase.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_ledger_survives_a_restart() -> None:
    """Say nothing and you get the durable one."""
    ledger = _cli().build_ledger(None)
    assert isinstance(ledger, FileSpendLedger), (
        "the default spend ledger is in-memory; a restart would hand the agent a fresh "
        "daily cap, which is the failure a rolling cap exists to prevent"
    )


def test_the_ephemeral_ledger_must_be_asked_for_by_name() -> None:
    """It remains available — for tests and one-shot runs — but never by accident."""
    assert isinstance(_cli().build_ledger(":memory:"), InMemorySpendLedger)


def test_the_default_path_is_named_and_stable(tmp_path: Path) -> None:
    """An explicit path is honoured, so an operator can point it at durable storage."""
    target = tmp_path / "spend-ledger.jsonl"
    ledger = _cli().build_ledger(str(target))
    assert isinstance(ledger, FileSpendLedger)
    assert ledger.path == str(target)
