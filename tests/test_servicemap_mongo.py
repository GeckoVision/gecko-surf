"""``MongoRegistry`` on the in-memory Mongo fake, and the import script.

No real store: ``FakeMongoClient`` stands in for pymongo, and the script's Mongo path
is replaced by one that fails, so a dry run that reached it would fail the test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.catalog_load import load_catalog
from gecko.servicemap import store_mongo
from gecko.servicemap.importer import import_files
from gecko.servicemap.mongo import MongoRegistry
from gecko.servicemap.registry import (
    MemoryRegistry,
    RegistryKilledError,
    RegistryUnavailableError,
)
from gecko.servicemap.snapshot import load_from_registry
from gecko.servicemap.types import Event
from tests.anyquery_fake_mongo import FakeMongoClient, FakeUnavailableError

FIXTURES = Path(__file__).parent / "fixtures" / "anyquery"
LEDGER = FIXTURES / "observed_fields.jsonl"
NOW = "2026-10-10T00:00:00+00:00"


def _records() -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / "catalog_details_fixture.json").read_text("utf-8"))
    return list(data["records"])


def _details_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "details"
    folder.mkdir()
    for n, record in enumerate(_records()):
        (folder / f"{n:03d}.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def _mongo(client: FakeMongoClient | None = None) -> MongoRegistry:
    return MongoRegistry(
        client or FakeMongoClient(), unavailable=(FakeUnavailableError,)
    )


def test_mongo_and_memory_give_the_planner_the_same_pool() -> None:
    mongo, memory = _mongo(), MemoryRegistry()
    for registry in (mongo, memory):
        import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW)

    from_mongo = load_from_registry(mongo, now=NOW)
    assert from_mongo == load_from_registry(memory, now=NOW)
    assert (
        from_mongo.candidates.snapshot_sha
        == load_catalog(_records(), ledgers=[LEDGER]).candidates.snapshot_sha
    )


def test_a_reimport_merges_in_place_rather_than_adding_rows() -> None:
    client = FakeMongoClient()
    registry = _mongo(client)
    import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW)
    sizes = {n: len(c.docs) for n, c in client[store_mongo.DB_NAME].collections.items()}

    import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW)

    assert {
        n: len(c.docs) for n, c in client[store_mongo.DB_NAME].collections.items()
    } == sizes


def test_events_are_appended_never_merged() -> None:
    registry = _mongo()
    event = Event(NOW, "price", "op", "svc", "orthogonal", "3", "4")
    registry.append_event(event)
    registry.append_event(event)
    assert registry.events() == [event, event]


def test_the_kill_switch_document_stops_every_write() -> None:
    client = FakeMongoClient()
    client[store_mongo.DB_NAME][store_mongo.CONTROL].insert_one(
        {"_id": store_mongo.KILL_SWITCH_ID, "engaged": True}
    )
    registry = _mongo(client)

    with pytest.raises(RegistryKilledError):
        import_files(registry, details=_records(), ledgers=[LEDGER], now=NOW)
    assert registry.sightings() == []


def test_an_unreachable_store_is_refused_without_the_connection_string() -> None:
    client = FakeMongoClient(down=True)
    with pytest.raises(RegistryUnavailableError) as refused:
        _mongo(client).services()
    assert "hunter2" not in str(refused.value) and "mongodb" not in str(refused.value)


class _RecordingCollection:
    def __init__(self, calls: list[tuple[str, list[Any], dict[str, Any]]], name: str):
        self.calls, self.name = calls, name

    def create_index(self, keys: list[Any], **options: Any) -> str:
        self.calls.append((self.name, keys, options))
        return str(options["name"])


class _RecordingDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any], dict[str, Any]]] = []

    def __getitem__(self, name: str) -> _RecordingCollection:
        return _RecordingCollection(self.calls, name)


def test_probes_expire_after_thirty_days_and_fields_are_unique_per_op() -> None:
    db = _RecordingDb()
    store_mongo.ensure_indexes(db)
    by_name = {(c, o["name"]): (k, o) for c, k, o in db.calls}

    keys, options = by_name[("probes", "expire")]
    assert options["expireAfterSeconds"] == 30 * 24 * 3600
    assert by_name[("fields", "one_per_op_path")][1]["unique"] is True
    names = {spec.name for spec in store_mongo.COLLECTIONS}
    assert names == {
        "services", "operations", "sightings", "fields", "offers", "equivalences",
        "health", "events", "probes", "control",
    }  # fmt: skip


# --- the script ----------------------------------------------------------------------------


def test_the_import_script_is_dry_by_default_and_reports_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import scripts.servicemap_import as script

    def no_mongo() -> MongoRegistry:
        raise AssertionError("a dry run reached Mongo")

    monkeypatch.setattr(script, "_mongo_registry", no_mongo)
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    (ledgers / "a.jsonl").write_text(LEDGER.read_text("utf-8"), encoding="utf-8")

    code = script.main(
        ["--details", str(_details_dir(tmp_path)), "--ledgers", str(ledgers)]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "DRY RUN" in out and "same: True" in out
    assert "sightings orthogonal: 20" in out


def test_mongo_reads_one_operations_offers_and_health() -> None:
    from gecko.servicemap.types import Health, OfferObs

    registry = _mongo()
    for op, pay_to in (("op-a", "A" * 32), ("op-b", "B" * 32)):
        registry.put_offer(
            OfferObs(
                offer_id=f"{op}-offer",
                op_id=op,
                scheme="exact",
                network="solana:x",
                asset="usdc",
                amount=1,
                pay_to=pay_to,
                max_timeout_seconds=60,
                fee_payer=None,
                evidence="EXTRACTED",
                first_seen=NOW,
                last_seen=NOW,
            )
        )
    registry.put_health(Health("op-a", "quarantined", 1, NOW, None, 402, 0, NOW))
    assert [o.pay_to for o in registry.offers_for("op-a")] == ["A" * 32]
    held = registry.health_for("op-a")
    assert held is not None and held.status == "quarantined"
    assert registry.health_for("op-b") is None
