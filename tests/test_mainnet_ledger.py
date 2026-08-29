"""The ledger writes the prediction beside the signature, and refuses to write a fork.

Offline: no RPC, no chain, no keys. What is under test is the recording discipline —
that a fork signature can never enter the mainnet record, that a broken path cannot fail
a send that already happened, and that the row never carries `charged_cu`.
"""

from __future__ import annotations

import json

import pytest

from gecko.mainnet_ledger import LEDGER_ENV, LedgerRow, default_path, record


def _row(**kw: object) -> LedgerRow:
    base = dict(
        signature="4X8dCyZUfake",
        predicted_cu=24_956,
        predicted_source="a test",
        network="mainnet",
    )
    base.update(kw)
    return LedgerRow(**base)  # type: ignore[arg-type]


def test_records_a_mainnet_row(tmp_path) -> None:
    target = tmp_path / "ledger.jsonl"
    assert record(_row(), path=target) == target
    row = json.loads(target.read_text(encoding="utf-8").strip())
    assert row["signature"] == "4X8dCyZUfake"
    assert row["predicted_cu"] == 24_956
    assert row["predicted_source"], "a prediction without a source is a memory"


@pytest.mark.parametrize("network", ["fork", "devnet", "testnet", "surfnet"])
def test_refuses_everything_that_is_not_mainnet(network: str, tmp_path) -> None:
    """A fork is not a compute oracle, and a fork signature in this file would be that
    error committed inside the artifact whose only job is being trustworthy."""
    target = tmp_path / "ledger.jsonl"
    assert record(_row(network=network), path=target) is None
    assert not target.exists()


def test_never_writes_charged_cu() -> None:
    """`charged_cu` is read back from the chain. A number we write and then check against
    itself proves nothing — the same self-referential trap sign_and_send documents."""
    assert "charged_cu" not in _row().to_json()


def test_an_unwritable_path_does_not_raise(tmp_path) -> None:
    """The transaction is already irreversible by the time we get here. A ledger that can
    abort makes recording more dangerous than not recording."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    assert record(_row(), path=blocked / "nested" / "ledger.jsonl") is None


def test_appends_rather_than_replaces(tmp_path) -> None:
    target = tmp_path / "ledger.jsonl"
    record(_row(signature="one"), path=target)
    record(_row(signature="two"), path=target)
    assert len(target.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(LEDGER_ENV, str(tmp_path / "elsewhere.jsonl"))
    assert default_path() == tmp_path / "elsewhere.jsonl"


def test_date_is_stamped_when_absent() -> None:
    assert _row().to_json()["date"]
