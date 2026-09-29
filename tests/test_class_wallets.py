"""Class-wallet registration: prove control of an address before the founder funds it.

Offline: a real ed25519 keypair generated here, the in-memory key registry, and the
in-memory class-wallet store. Nothing touches a network or a chain.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from solders.keypair import Keypair
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gecko.keyregistry import InMemoryKeyRegistry, hash_key, mint_key
from gecko.registry.api import registry_routes
from gecko.registry.class_wallet_store import (
    InMemoryClassWalletStore,
    MongoClassWalletStore,
)
from gecko.registry.class_wallets import (
    BOOTCAMP_SURFACE,
    CHALLENGE_TTL,
    ClassWalletRegistrar,
    FundingRow,
    funding_plan,
)
from gecko.registry.store import SurfaceStore

COHORT = "2026-09"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def _clean_throttle():
    from gecko.registry import api as _api

    _api._class_wallet_ip_counts.clear()
    yield
    _api._class_wallet_ip_counts.clear()


class Env:
    def __init__(self, store: Any = None) -> None:
        self.keys = InMemoryKeyRegistry()
        self.store = store if store is not None else InMemoryClassWalletStore()
        self.clock = Clock()
        self.registrar = ClassWalletRegistrar.from_key_registry(
            self.keys, self.store, clock=self.clock
        )
        app = Starlette(
            routes=registry_routes(SurfaceStore([]), None, class_wallets=self.registrar)
        )
        self.client = TestClient(app)

    def key(self, account: str, *, granted: bool = True) -> str:
        plain = mint_key()
        self.keys.store_key(
            key_hash=hash_key(plain),
            account_id=account,
            label="test",
            surfaces=[BOOTCAMP_SURFACE] if granted else [],
        )
        return plain

    def challenge(self, key: str) -> dict[str, Any]:
        r = self.client.get(
            "/registry/class-wallet/challenge",
            params={"cohort": COHORT},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def register(
        self, key: str, kp: Keypair, challenge: str, *, signer: Keypair | None = None
    ) -> Any:
        sig = (signer or kp).sign_message(challenge.encode("utf-8"))
        return self.client.post(
            "/registry/class-wallet",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "cohort": COHORT,
                "address": str(kp.pubkey()),
                "challenge": challenge,
                "signature": str(sig),
            },
        )


def test_happy_path_registers_a_signed_address():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    assert issued["challenge"].startswith(
        f"dev3pack {COHORT} class wallet for acct-alice nonce "
    )
    assert f" expires {issued['expires_at']}" in issued["challenge"]
    kp = Keypair()
    r = env.register(key, kp, issued["challenge"])
    assert r.status_code == 200, r.text
    assert r.json() == {
        "account": "acct-alice",
        "cohort": COHORT,
        "address": str(kp.pubkey()),
        "registered": True,
        "replaced": False,
    }
    assert [w.address for w in env.store.list_wallets(COHORT)] == [str(kp.pubkey())]


def test_challenge_expires_after_ten_minutes():
    env = Env()
    issued = env.challenge(env.key("acct-alice"))
    expires = datetime.fromisoformat(issued["expires_at"].replace("Z", "+00:00"))
    assert expires - env.clock.now == CHALLENGE_TTL == timedelta(minutes=10)


def test_wrong_signature_is_refused():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    r = env.register(key, Keypair(), issued["challenge"], signer=Keypair())
    assert r.status_code == 400
    assert r.json()["code"] == "signature-invalid"
    assert env.store.list_wallets(COHORT) == []


def test_signature_over_a_different_message_is_refused():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    kp = Keypair()
    r = env.client.post(
        "/registry/class-wallet",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "cohort": COHORT,
            "address": str(kp.pubkey()),
            "challenge": issued["challenge"],
            "signature": str(kp.sign_message(b"something else")),
        },
    )
    assert r.status_code == 400 and r.json()["code"] == "signature-invalid"


def test_challenge_issued_to_another_account_is_refused():
    env = Env()
    alice, bob = env.key("acct-alice"), env.key("acct-bob")
    issued = env.challenge(alice)
    r = env.register(bob, Keypair(), issued["challenge"])
    assert r.status_code == 400
    assert r.json()["code"] == "challenge-invalid"
    # Bob's attempt did not burn Alice's challenge.
    assert env.register(alice, Keypair(), issued["challenge"]).status_code == 200


def test_challenge_for_another_cohort_is_refused():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    kp = Keypair()
    r = env.client.post(
        "/registry/class-wallet",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "cohort": "2026-10",
            "address": str(kp.pubkey()),
            "challenge": issued["challenge"],
            "signature": str(kp.sign_message(issued["challenge"].encode())),
        },
    )
    assert r.status_code == 400 and r.json()["code"] == "challenge-invalid"


def test_expired_challenge_is_refused():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    env.clock.now += CHALLENGE_TTL + timedelta(seconds=1)
    r = env.register(key, Keypair(), issued["challenge"])
    assert r.status_code == 400
    assert r.json()["code"] == "challenge-invalid"


def test_replayed_challenge_is_refused():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    kp = Keypair()
    assert env.register(key, kp, issued["challenge"]).status_code == 200
    r = env.register(key, kp, issued["challenge"])
    assert r.status_code == 400
    assert r.json()["code"] == "challenge-invalid"


def test_fabricated_challenge_is_refused():
    env = Env()
    key = env.key("acct-alice")
    made_up = (
        f"dev3pack {COHORT} class wallet for acct-alice nonce 00 "
        "expires 2099-01-01T00:00:00Z"
    )
    r = env.register(key, Keypair(), made_up)
    assert r.status_code == 400 and r.json()["code"] == "challenge-invalid"


def test_invalid_address_is_refused():
    env = Env()
    key = env.key("acct-alice")
    issued = env.challenge(key)
    r = env.client.post(
        "/registry/class-wallet",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "cohort": COHORT,
            "address": "not-a-pubkey",
            "challenge": issued["challenge"],
            "signature": "1" * 64,
        },
    )
    assert r.status_code == 400 and r.json()["code"] == "address-invalid"


@pytest.mark.parametrize("header", [None, "Bearer ", "Bearer gecko_sk_nope", "Basic x"])
def test_missing_or_invalid_key_is_401(header):
    env = Env()
    headers = {"Authorization": header} if header is not None else {}
    r = env.client.get(
        "/registry/class-wallet/challenge", params={"cohort": COHORT}, headers=headers
    )
    assert r.status_code == 401 and r.json()["code"] == "key-invalid"
    r = env.client.post("/registry/class-wallet", headers=headers, json={})
    assert r.status_code == 401 and r.json()["code"] == "key-invalid"


def test_not_granted_is_403():
    env = Env()
    key = env.key("acct-carol", granted=False)
    r = env.client.get(
        "/registry/class-wallet/challenge",
        params={"cohort": COHORT},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 403 and r.json()["code"] == "not-granted"
    r = env.client.post(
        "/registry/class-wallet",
        headers={"Authorization": f"Bearer {key}"},
        json={"cohort": COHORT},
    )
    assert r.status_code == 403 and r.json()["code"] == "not-granted"


def test_errors_never_echo_the_key():
    env = Env()
    key = env.key("acct-alice")
    bad = key + "x"
    for r in (
        env.client.get(
            "/registry/class-wallet/challenge",
            params={"cohort": COHORT},
            headers={"Authorization": f"Bearer {bad}"},
        ),
        env.register(key, Keypair(), "bogus"),
    ):
        assert key not in r.text and bad not in r.text


def test_re_register_replaces_the_address():
    env = Env()
    key = env.key("acct-alice")
    first, second = Keypair(), Keypair()
    assert (
        env.register(key, first, env.challenge(key)["challenge"]).json()["replaced"]
        is False
    )
    r = env.register(key, second, env.challenge(key)["challenge"])
    assert r.status_code == 200 and r.json()["replaced"] is True
    assert [w.address for w in env.store.list_wallets(COHORT)] == [str(second.pubkey())]


def test_no_key_or_signature_is_stored():
    fake = FakeMongo()
    env = Env(store=MongoClassWalletStore(fake.wallets, fake.challenges))
    key = env.key("acct-alice")
    issued = env.challenge(key)
    kp = Keypair()
    sig = str(kp.sign_message(issued["challenge"].encode()))
    assert env.register(key, kp, issued["challenge"]).status_code == 200
    dump = json.dumps(
        [fake.wallets.docs, fake.challenges.docs], default=str, sort_keys=True
    )
    assert key not in dump and hash_key(key) not in dump
    assert sig not in dump
    # The consumed challenge is gone; the wallet record carries exactly the contract.
    assert fake.challenges.docs == {}
    (doc,) = fake.wallets.docs.values()
    assert set(doc) - {"_id"} == {
        "account",
        "cohort",
        "address",
        "challenge_nonce_used",
        "verified_at",
    }


def test_mongo_store_round_trip_matches_the_in_memory_store():
    fake = FakeMongo()
    env = Env(store=MongoClassWalletStore(fake.wallets, fake.challenges))
    key = env.key("acct-alice")
    kp = Keypair()
    assert env.register(key, kp, env.challenge(key)["challenge"]).status_code == 200
    r = env.register(key, Keypair(), env.challenge(key)["challenge"])
    assert r.json()["replaced"] is True
    assert len(env.store.list_wallets(COHORT)) == 1
    assert env.store.delete_wallet("acct-alice", COHORT) is True
    assert env.store.delete_wallet("acct-alice", COHORT) is False


def test_routes_503_when_not_enabled():
    client = TestClient(Starlette(routes=registry_routes(SurfaceStore([]), None)))
    r = client.get("/registry/class-wallet/challenge", params={"cohort": COHORT})
    assert r.status_code == 503 and r.json()["code"] == "not-enabled"
    r = client.post("/registry/class-wallet", json={})
    assert r.status_code == 503 and r.json()["code"] == "not-enabled"


def test_per_ip_throttle_returns_429():
    from gecko.registry import api as _api

    env = Env()
    for _ in range(_api._CLASS_WALLET_MAX_PER_HOUR):
        env.client.get("/registry/class-wallet/challenge", params={"cohort": COHORT})
    r = env.client.get("/registry/class-wallet/challenge", params={"cohort": COHORT})
    assert r.status_code == 429 and r.json()["code"] == "rate-limited"


def test_funding_plan_arithmetic():
    wallets = [
        FundingRow(account="a", address="A1"),
        FundingRow(account="b", address="B1"),
    ]
    plan = funding_plan(wallets, price_raw=100_000, count=3, sol_lamports=9_400_000)
    assert [(r.account, r.usdc_raw, r.sol_lamports) for r in plan.rows] == [
        ("a", 300_000, 9_400_000),
        ("b", 300_000, 9_400_000),
    ]
    assert plan.total_usdc_raw == 600_000
    assert plan.total_sol_lamports == 18_800_000


def test_list_script_prints_rows_and_totals(tmp_path, capsys):
    from scripts import class_wallets as script

    export = tmp_path / "wallets.json"
    export.write_text(
        json.dumps(
            [
                {"account": "a", "cohort": COHORT, "address": "Addr1"},
                {"account": "b", "cohort": COHORT, "address": "Addr2"},
                {"account": "c", "cohort": "2026-10", "address": "Other"},
            ]
        )
    )
    assert script.main(["list", "--cohort", COHORT, "--json", str(export)]) == 0
    out = capsys.readouterr().out
    assert "Addr1" in out and "Addr2" in out and "Other" not in out
    assert "600000" in out and "18800000" in out
    assert "0.600000 USDC" in out and "0.018800000 SOL" in out


# --- a minimal duck-typed Mongo collection: equality filters only ------------------


class FakeCollection:
    def __init__(self) -> None:
        self.docs: dict[Any, dict[str, Any]] = {}
        self._auto = 0

    @staticmethod
    def _match(doc: dict[str, Any], flt: dict[str, Any]) -> bool:
        return all(doc.get(k) == v for k, v in flt.items())

    def insert_one(self, doc: dict[str, Any]) -> None:
        doc = dict(doc)
        if "_id" not in doc:
            self._auto += 1
            doc["_id"] = self._auto
        assert doc["_id"] not in self.docs
        self.docs[doc["_id"]] = doc

    def find_one_and_delete(self, flt: dict[str, Any]) -> dict[str, Any] | None:
        for k, doc in list(self.docs.items()):
            if self._match(doc, flt):
                return self.docs.pop(k)
        return None

    def find_one_and_update(
        self, flt: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> dict[str, Any] | None:
        for doc in self.docs.values():
            if self._match(doc, flt):
                before = dict(doc)
                doc.update(update["$set"])
                return before
        if upsert:
            self.insert_one({**flt, **update["$set"]})
        return None

    def find(
        self, flt: dict[str, Any], projection: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [dict(d) for d in self.docs.values() if self._match(d, flt)]

    def delete_one(self, flt: dict[str, Any]) -> Any:
        hit = self.find_one_and_delete(flt)

        class _R:
            deleted_count = 1 if hit else 0

        return _R()

    def create_index(self, *args: Any, **kwargs: Any) -> str:
        return "ok"


class FakeMongo:
    def __init__(self) -> None:
        self.wallets = FakeCollection()
        self.challenges = FakeCollection()
