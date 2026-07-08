"""Hosted wiring: no env -> issuance disabled; never crashes the server."""

import gecko.registry.wiring as wiring


def test_no_env_returns_none(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("GECKO_OTP_FROM", raising=False)
    assert wiring.build_keystore_from_env() is None


def test_mongo_without_mailer_returns_none(monkeypatch):
    monkeypatch.setenv(
        "MONGODB_URI", "mongodb://localhost:1/x?serverSelectionTimeoutMS=10"
    )
    monkeypatch.delenv("GECKO_OTP_FROM", raising=False)
    assert wiring.build_keystore_from_env() is None
