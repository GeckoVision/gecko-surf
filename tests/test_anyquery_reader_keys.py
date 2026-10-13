"""Reader keys: one key, one dataset, read only, never a contact detail in the clear.

A grant on the owner's account is per ACCOUNT (``set_account_surfaces`` rewrites every
key of it). A reader key must stay out of that join, so a re-grant of the owner can
never hand it the paying tools. Fully offline: in-memory registries and the fake Mongo.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

from gecko.anyquery.datasets import new_dataset
from gecko.anyquery.reader_keys import (
    READER_KEY_PREFIX,
    DatasetAccess,
    InMemoryReaderKeyRegistry,
    MongoReaderKeyRegistry,
    ReaderKeyError,
    mint_reader_key,
    resolve_dataset_access,
    revoke_reader_key,
    shown_value,
)
from gecko.anyquery.store import MemoryStore
from gecko.keyauth import KeyGate, scope_gate
from gecko.keyregistry import (
    GeckoKeyResolver,
    InMemoryKeyRegistry,
    RegistryAllowlist,
    hash_key,
    mint_key,
)
from tests.anyquery_dataset_fixtures import ACCT, NOW, OTHER, plan
from tests.anyquery_fake_mongo import FakeMongoClient

LEADS = "lead-contacts"
OTHER_DS = "board-members"


def _store() -> MemoryStore:
    store = MemoryStore()
    for customer in (ACCT, OTHER):
        for ds in (LEADS, OTHER_DS):
            store.create_dataset(
                new_dataset(
                    plan(customer), dataset_id=ds, bundle=LEADS, mode="live", now=NOW
                )
            )
    return store


class _World:
    """The owner's account key, one reader key on ``lead-contacts``, and the gates."""

    def __init__(self, readers: Any = None) -> None:
        self.keys = InMemoryKeyRegistry()
        self.readers = readers if readers is not None else InMemoryReaderKeyRegistry()
        self.store = _store()
        self.owner_key = mint_key()
        self.keys.store_key(
            key_hash=hash_key(self.owner_key),
            account_id=ACCT,
            label="owner",
            surfaces=["anyquery"],
        )
        self.allow = RegistryAllowlist(self.keys)
        self.reader_key, self.grant = mint_reader_key(
            self.readers, self.store, owner=ACCT, dataset_id=LEADS, label="analyst"
        )

    def access(self, token: str) -> DatasetAccess | None:
        return resolve_dataset_access(
            token,
            readers=self.readers,
            resolve_owner=GeckoKeyResolver(self.keys),
            owner_enabled=self.allow.is_enabled,
        )


def test_reader_key_reads_only_its_own_dataset() -> None:
    world = _World()
    access = world.access(world.reader_key)
    assert access is not None
    assert access.role == "reader"
    assert access.customer == ACCT
    assert access.may_read(ACCT, LEADS)
    assert not access.may_read(ACCT, OTHER_DS)  # same owner, other dataset
    assert not access.may_read(OTHER, LEADS)  # same slug, other customer
    assert not access.may_read(OTHER, OTHER_DS)


def test_owner_key_reads_every_dataset_of_its_own_and_none_of_another() -> None:
    world = _World()
    access = world.access(world.owner_key)
    assert access is not None and access.role == "owner"
    assert access.may_read(ACCT, LEADS) and access.may_read(ACCT, OTHER_DS)
    assert not access.may_read(OTHER, LEADS)


def test_only_the_owner_key_may_reveal_contact_details() -> None:
    world = _World()
    owner = world.access(world.owner_key)
    reader = world.access(world.reader_key)
    assert owner is not None and reader is not None
    assert owner.may_reveal_pii and not reader.may_reveal_pii
    phone = "+44 20 7946 0001"
    # The reader asks to reveal and still gets the mask; the owner gets it only on ask.
    assert shown_value(reader, "mobile_phone", phone, reveal=True) != (phone, False)
    assert shown_value(reader, "mobile_phone", phone, reveal=True)[1] is True
    assert shown_value(owner, "mobile_phone", phone, reveal=False)[1] is True
    assert shown_value(owner, "mobile_phone", phone, reveal=True) == (phone, False)


def test_reader_key_never_passes_the_paying_gate() -> None:
    world = _World()
    gate = scope_gate(
        KeyGate(resolve_account=GeckoKeyResolver(world.keys), allowlist=world.allow),
        "anyquery",
    )
    assert gate.decide(world.owner_key).allowed
    denied = gate.decide(world.reader_key)
    assert not denied.allowed and denied.account is None
    assert GeckoKeyResolver(world.keys)(world.reader_key) is None


def test_reader_key_survives_an_owner_regrant_without_gaining_surfaces() -> None:
    world = _World()
    before = world.access(world.reader_key)
    world.keys.set_account_surfaces(ACCT, ["anyquery", "birdeye", "xstock"])
    world.allow.grant(ACCT, "orthogonal")
    after = world.access(world.reader_key)
    assert after == before
    assert after is not None and after.dataset_id == LEADS
    assert world.readers.readers_for(ACCT) == [world.grant]
    for surface in ("anyquery", "birdeye", "xstock", "orthogonal"):
        gate = scope_gate(
            KeyGate(
                resolve_account=GeckoKeyResolver(world.keys), allowlist=world.allow
            ),
            surface,
        )
        assert not gate.decide(world.reader_key).allowed


def test_revoked_reader_key_is_refused() -> None:
    world = _World()
    assert revoke_reader_key(world.readers, owner=ACCT, key_id=world.grant.key_id)
    assert world.access(world.reader_key) is None
    # Idempotent: a second revoke changes nothing.
    assert not revoke_reader_key(world.readers, owner=ACCT, key_id=world.grant.key_id)


def test_another_owner_cannot_revoke_my_reader_key() -> None:
    world = _World()
    assert not revoke_reader_key(world.readers, owner=OTHER, key_id=world.grant.key_id)
    assert world.access(world.reader_key) is not None


def test_reader_key_dies_with_its_owners_account() -> None:
    world = _World()
    world.allow.disable(ACCT)
    assert world.access(world.reader_key) is None
    world.allow.enable(ACCT)
    assert world.access(world.reader_key) is not None


def test_mint_refuses_a_dataset_the_owner_does_not_hold() -> None:
    world = _World()
    with pytest.raises(ReaderKeyError):
        mint_reader_key(
            world.readers, world.store, owner=ACCT, dataset_id="nope", label="x"
        )
    with pytest.raises(ReaderKeyError):
        mint_reader_key(
            world.readers, world.store, owner=ACCT, dataset_id="Bad Slug", label="x"
        )
    with pytest.raises(ReaderKeyError):
        mint_reader_key(
            world.readers, world.store, owner=" ", dataset_id=LEADS, label="x"
        )


def test_unknown_or_foreign_tokens_resolve_to_nothing() -> None:
    world = _World()
    assert world.access("") is None
    assert world.access(READER_KEY_PREFIX + "x" * 43) is None
    assert world.access("not-a-key") is None


def test_the_key_is_hashed_at_rest_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeMongoClient()
    collection = client["gecko_registry"]["gecko_reader_keys"]
    with caplog.at_level(logging.DEBUG):
        world = _World(readers=MongoReaderKeyRegistry(collection))
        assert world.access(world.reader_key) is not None
        assert revoke_reader_key(world.readers, owner=ACCT, key_id=world.grant.key_id)
        assert world.access(world.reader_key) is None
    assert world.reader_key.startswith(READER_KEY_PREFIX)
    assert world.reader_key not in repr(collection.docs)
    assert world.reader_key not in repr(world.readers)
    assert world.reader_key not in repr(world.grant)
    assert world.reader_key not in caplog.text
    (doc,) = collection.docs
    assert doc["_id"] == hash_key(world.reader_key)
    assert "reveal" not in repr(doc)  # a reader record has no reveal switch at all


def test_a_registry_error_fails_closed() -> None:
    class Broken(InMemoryReaderKeyRegistry):
        def reader_for(self, key_hash: str) -> Any:
            raise RuntimeError("down")

    world = _World(readers=Broken())
    assert world.access(world.reader_key) is None


def test_no_mcp_surface_can_mint_a_key() -> None:
    root = Path(__file__).resolve().parents[1] / "gecko"
    surfaces = [
        root / "mcp_server.py",
        root / "http_server.py",
        *(root / "anyquery").glob("*.py"),
    ]
    for path in surfaces:
        if path.name == "reader_keys.py":
            continue
        assert "mint_reader_key" not in path.read_text(), path


# --- the owner-only script --------------------------------------------------------

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dataset_reader_keys.py"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("dataset_reader_keys_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(world: _World, argv: list[str], owner_key: str) -> int:
    return int(
        _script().main(
            argv,
            store=world.store,
            keys=world.keys,
            readers=world.readers,
            owner_key=owner_key,
        )
    )


def test_script_mints_for_the_account_whose_key_is_presented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = _World()
    assert _run(world, ["mint", "--dataset", OTHER_DS], world.owner_key) == 0
    out = capsys.readouterr().out
    (key,) = [w for w in out.split() if w.startswith(READER_KEY_PREFIX)]
    access = world.access(key)
    assert access is not None and access.customer == ACCT
    assert access.dataset_id == OTHER_DS


def test_script_refuses_a_reader_key_or_a_bad_key_as_the_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = _World()
    assert _run(world, ["mint", "--dataset", LEADS], world.reader_key) != 0
    assert _run(world, ["mint", "--dataset", LEADS], "gecko_sk_" + "z" * 43) != 0
    out = capsys.readouterr()
    assert world.reader_key not in out.out + out.err
    assert len(world.readers.readers_for(ACCT)) == 1


def test_script_lists_and_revokes_without_printing_a_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = _World()
    assert _run(world, ["list"], world.owner_key) == 0
    listed = capsys.readouterr().out
    assert world.grant.key_id in listed and world.reader_key not in listed
    assert world.owner_key not in listed
    assert _run(world, ["revoke", "--key-id", world.grant.key_id], world.owner_key) == 0
    assert world.access(world.reader_key) is None
