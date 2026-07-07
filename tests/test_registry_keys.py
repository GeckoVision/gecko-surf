"""Key issuance: email OTP -> gk_live_ key; hash-only at rest; abuse caps."""

from typing import Any

import pytest

from gecko.registry.keys import (
    OTP_MAX_ATTEMPTS,
    OTP_MAX_PER_HOUR,
    OTP_TTL_SECONDS,
    KeyStore,
    RegistryAuthError,
)


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def insert_one(self, doc: dict[str, Any]) -> None:
        self.docs.append(dict(doc))

    def find_one(self, q: dict[str, Any]) -> dict[str, Any] | None:
        for d in reversed(self.docs):
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None

    def find(self, q: dict[str, Any]) -> list[dict[str, Any]]:
        if not q:
            return [dict(d) for d in self.docs]
        return [dict(d) for d in self.docs if all(d.get(k) == v for k, v in q.items())]

    def update_one(self, q: dict[str, Any], u: dict[str, Any]) -> None:
        for d in reversed(self.docs):
            if all(d.get(k) == v for k, v in q.items()):
                d.update(u.get("$set", {}))
                for k, n in u.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + n
                return

    def update_many(self, q: dict[str, Any], u: dict[str, Any]) -> None:
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                d.update(u.get("$set", {}))
                for k, n in u.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + n

    def count_documents(self, q: dict[str, Any]) -> int:
        gte = {
            k: v["$gte"] for k, v in q.items() if isinstance(v, dict) and "$gte" in v
        }
        eq = {k: v for k, v in q.items() if not isinstance(v, dict)}
        n = 0
        for d in self.docs:
            if all(d.get(k) == v for k, v in eq.items()) and all(
                d.get(k, 0) >= v for k, v in gte.items()
            ):
                n += 1
        return n

    def delete_many(self, q: dict[str, Any]) -> None:
        self.docs = [
            d for d in self.docs if not all(d.get(k) == v for k, v in q.items())
        ]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _store() -> tuple[KeyStore, list[tuple[str, str]], Clock]:
    sent: list[tuple[str, str]] = []
    clock = Clock()
    ks = KeyStore(
        keys_collection=FakeCollection(),
        otp_collection=FakeCollection(),
        mailer=lambda email, code: sent.append((email, code)),
        clock=clock,
    )
    return ks, sent, clock


def test_otp_roundtrip_issues_key_and_stores_hash_only():
    ks, sent, _ = _store()
    ks.start_otp("dev@example.com")
    assert len(sent) == 1 and sent[0][0] == "dev@example.com"
    code = sent[0][1]
    assert len(code) == 6 and code.isdigit()
    key = ks.verify_otp("dev@example.com", code)
    assert key.startswith("gk_live_")
    # hash-only at rest: the plaintext never appears in any stored doc
    for coll in (ks._keys, ks._otps):
        for doc in coll.docs:
            assert key not in str(doc)
    # the key authenticates
    rec = ks.check(key)
    assert rec is not None and rec["email"] == "dev@example.com"
    assert "hash" not in rec and "salt" not in rec


def test_wrong_otp_limited_attempts():
    ks, sent, _ = _store()
    ks.start_otp("dev@example.com")
    for _ in range(OTP_MAX_ATTEMPTS):
        with pytest.raises(RegistryAuthError):
            ks.verify_otp("dev@example.com", "000000")
    # even the right code is dead after max attempts
    with pytest.raises(RegistryAuthError):
        ks.verify_otp("dev@example.com", sent[0][1])


def test_otp_expires():
    ks, sent, clock = _store()
    ks.start_otp("dev@example.com")
    clock.now += OTP_TTL_SECONDS + 1
    with pytest.raises(RegistryAuthError):
        ks.verify_otp("dev@example.com", sent[0][1])


def test_issuance_rate_limited_per_email():
    ks, _, _ = _store()
    for _ in range(OTP_MAX_PER_HOUR):
        ks.start_otp("dev@example.com")
    with pytest.raises(RegistryAuthError):
        ks.start_otp("dev@example.com")


def test_check_unknown_key_returns_none():
    ks, _, _ = _store()
    assert ks.check("gk_live_nope") is None


def test_new_otp_supersedes_old_one():
    ks, sent, _ = _store()
    ks.start_otp("dev@example.com")
    ks.start_otp("dev@example.com")
    with pytest.raises(RegistryAuthError):
        ks.verify_otp("dev@example.com", sent[0][1])  # first code is dead
    key = ks.verify_otp("dev@example.com", sent[1][1])
    assert key.startswith("gk_live_")


def test_otp_not_stored_in_plaintext():
    ks, sent, _ = _store()
    ks.start_otp("dev@example.com")
    assert sent[0][1] not in str(ks._otps.docs)


def test_email_normalized():
    ks, sent, _ = _store()
    ks.start_otp("  Dev@Example.COM ")
    key = ks.verify_otp("dev@example.com", sent[0][1])
    assert ks.check(key)["email"] == "dev@example.com"
