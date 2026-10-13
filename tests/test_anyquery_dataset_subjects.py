"""A subject that is itself a contact detail is masked like a value (12 Oct ruling).

"Enrich these emails" is a normal ask, so a subject may be an email or a phone number.
Only the owner's key, asking, sees it whole: in the schema, the rows, the diff and both
downloads. Offline: MemoryStore and the fake Mongo, in-memory key registries.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from gecko.anyquery.dataset_download import Download, dataset_download
from gecko.anyquery.dataset_read import DatasetCaller
from gecko.anyquery.dataset_surface import call_dataset_tool
from gecko.anyquery.datasets import DatasetDeps, adopt_run, new_dataset
from gecko.anyquery.reader_keys import (
    InMemoryReaderKeyRegistry,
    mint_reader_key,
    resolve_dataset_access,
)
from gecko.anyquery.types import Bundle
from gecko.keyregistry import (
    GeckoKeyResolver,
    InMemoryKeyRegistry,
    RegistryAllowlist,
    hash_key,
    mint_key,
)
from tests.anyquery_dataset_fixtures import ACCT, NOW, R1, R2, STORES, fact, plan, run

DS = "email-leads"
EMAIL = "jane.doe@corp.example"
PHONE_SUBJECT = "+44 20 7946 0002"
CONTACTS = (EMAIL, PHONE_SUBJECT)
PLAIN = ("ada lovelace",)


class ContactWorld:
    def __init__(self, store_kind: str) -> None:
        self.store = STORES[store_kind]()
        held = plan(subjects=CONTACTS + PLAIN)
        self.store.save_plan(held)
        self.store.save_bundle(Bundle(ACCT, DS, held.plan_id, held.query, NOW))
        self.store.start_run(run(R1))
        self.store.write_facts(
            ACCT,
            [
                fact(R1, s, "job_title", "repaired", f"title of {i}")
                for i, s in enumerate(CONTACTS + PLAIN)
            ],
        )
        self.store.create_dataset(
            new_dataset(held, dataset_id=DS, bundle=DS, mode="live", now=NOW)
        )
        adopt_run(DatasetDeps(store=self.store), ACCT, DS, R1)
        self.keys = InMemoryKeyRegistry()
        self.owner_key = mint_key()
        self.keys.store_key(
            key_hash=hash_key(self.owner_key),
            account_id=ACCT,
            label="owner",
            surfaces=["anyquery"],
        )
        self.readers = InMemoryReaderKeyRegistry()
        self.reader_key, _ = mint_reader_key(
            self.readers, self.store, owner=ACCT, dataset_id=DS, label="analyst"
        )

    def caller(self, token: str) -> DatasetCaller:
        access = resolve_dataset_access(
            token,
            readers=self.readers,
            resolve_owner=GeckoKeyResolver(self.keys),
            owner_enabled=RegistryAllowlist(self.keys).is_enabled,
        )
        return DatasetCaller(DS, access)

    def call(self, token: str, name: str, **args: Any) -> dict[str, Any]:
        return call_dataset_tool(self.store, self.caller(token), name, args)

    def second_version(self) -> None:
        self.store.start_run(run(R2))
        self.store.write_facts(
            ACCT, [fact(R2, s, "job_title", "repaired", "new title") for s in CONTACTS]
        )
        adopt_run(DatasetDeps(store=self.store), ACCT, DS, R2)


@pytest.fixture(params=sorted(STORES))
def cw(request: pytest.FixtureRequest) -> ContactWorld:
    return ContactWorld(request.param)


def _leaks(answer: Any) -> list[str]:
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return [s for s in CONTACTS if s in text]


def test_a_reader_never_sees_a_contact_subject_whole(cw: ContactWorld) -> None:
    schema = cw.call(cw.reader_key, "schema")
    assert _leaks(schema) == [], schema
    assert "ada lovelace" in schema["subjects"]  # a name is not a contact detail
    assert len(schema["subjects"]) == 3

    rows = cw.call(cw.reader_key, "rows", reveal_pii=True)
    assert _leaks(rows) == [] and rows["pii_masked"] is True
    assert {r["subject"] for r in rows["rows"]} == set(schema["subjects"])

    cw.second_version()
    changes = cw.call(cw.reader_key, "changes_since", version=1, reveal_pii=True)
    assert len(changes["changes"]) == 2
    assert _leaks(changes) == [] and changes["pii_masked"] is True


def test_a_reader_download_masks_the_subject_column(cw: ContactWorld) -> None:
    got = dataset_download(cw.store, cw.caller(cw.reader_key), "json", reveal=True)
    assert isinstance(got, Download)
    body = got.body.decode()
    assert _leaks(body) == [] and json.loads(body)["pii_masked"] is True
    pq = pytest.importorskip("pyarrow.parquet")
    got = dataset_download(cw.store, cw.caller(cw.reader_key), "parquet", reveal=True)
    assert isinstance(got, Download)
    subjects = {r["subject"] for r in pq.read_table(io.BytesIO(got.body)).to_pylist()}
    assert not subjects & set(CONTACTS) and "ada lovelace" in subjects


def test_only_the_owner_asking_sees_a_contact_subject_whole(cw: ContactWorld) -> None:
    assert _leaks(cw.call(cw.owner_key, "rows")) == []
    whole = cw.call(cw.owner_key, "rows", reveal_pii=True)
    assert {r["subject"] for r in whole["rows"]} == set(CONTACTS + PLAIN)
    got = dataset_download(cw.store, cw.caller(cw.owner_key), "json", reveal=True)
    assert isinstance(got, Download) and sorted(_leaks(got.body.decode())) == sorted(
        CONTACTS
    )


def test_a_reader_filters_by_the_subject_it_was_shown_not_the_raw_one(
    cw: ContactWorld,
) -> None:
    shown = cw.call(cw.reader_key, "schema")["subjects"]
    masked_email = next(s for s in shown if s.endswith("@corp.example"))
    one = cw.call(cw.reader_key, "rows", subject=masked_email)
    assert [r["subject"] for r in one["rows"]] == [masked_email]
    # The raw value would make the filter an oracle: "is jane in this dataset?"
    assert cw.call(cw.reader_key, "rows", subject=EMAIL)["code"] == "unknown_subject"
    # The owner may name it whole.
    owned = cw.call(cw.owner_key, "rows", subject=EMAIL, reveal_pii=True)
    assert [r["subject"] for r in owned["rows"]] == [EMAIL]
