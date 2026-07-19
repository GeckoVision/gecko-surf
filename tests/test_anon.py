"""Anon-first identity — the client-side header logic + the opaque, PII-free hash.

Offline, no network: ``anon_connect_headers`` reads the persistent install id (and any
sealed login identity) under a tmp HOME and returns the headers our CLI attaches to a
hosted connect. Proves the four guardrails: stable per install, distinct per fresh HOME,
killed by ``GECKO_TELEMETRY=off``, and PII-free.
"""

from __future__ import annotations

import json
from pathlib import Path

from gecko.anon import (
    ACCOUNT_HEADER,
    ANON_HEADER,
    account_hash,
    anon_connect_headers,
)


def _write_identity(home: Path, identity: dict[str, str]) -> None:
    gecko = home / ".gecko"
    gecko.mkdir(parents=True, exist_ok=True)
    (gecko / "identity.json").write_text(json.dumps(identity), encoding="utf-8")


def test_same_install_carries_the_same_anon_id_and_account_hash(tmp_path):
    # Two connects from the SAME install carry the same X-Gecko-Anon, so the server's
    # hash of it (the `account`) is identical -> one person, many visits.
    first = anon_connect_headers(tmp_path)
    second = anon_connect_headers(tmp_path)
    assert first[ANON_HEADER] == second[ANON_HEADER]
    assert account_hash(first[ANON_HEADER]) == account_hash(second[ANON_HEADER])
    assert ACCOUNT_HEADER not in first  # no login yet -> anon only


def test_fresh_home_is_a_different_person(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    a = anon_connect_headers(home_a)[ANON_HEADER]
    b = anon_connect_headers(home_b)[ANON_HEADER]
    assert a != b
    assert account_hash(a) != account_hash(b)


def test_telemetry_off_emits_no_header(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_TELEMETRY", "off")
    assert anon_connect_headers(tmp_path) == {}
    # ...and no install id file is even created (nothing is read when opted out).
    assert not (tmp_path / ".gecko" / "install_id").exists()


def test_login_present_adds_the_account_header_no_pii(tmp_path):
    _write_identity(
        tmp_path, {"email": "dev@example.com", "issuer": "privy", "privy_user_id": "u1"}
    )
    headers = anon_connect_headers(tmp_path)
    assert headers[ANON_HEADER]  # still anonymous-first
    account = headers[ACCOUNT_HEADER]
    assert account.startswith("acct-")
    # The login hash is opaque: no raw email / user id / issuer leaks onto the wire.
    assert "dev@example.com" not in account
    assert "u1" not in account


def test_account_hash_is_opaque_and_stable():
    h = account_hash("privy:dev@example.com")
    assert h.startswith("acct-")
    assert "dev@example.com" not in h
    assert h == account_hash("privy:dev@example.com")  # stable
    assert h != account_hash("privy:other@example.com")


def test_login_upgrade_is_stable_across_sessions(tmp_path):
    # The whole point of login: the durable account hash is the SAME across visits, so a
    # person is linkable even as their anon id / session rotates.
    _write_identity(tmp_path, {"email": "d@x.io", "issuer": "registry"})
    a = anon_connect_headers(tmp_path)[ACCOUNT_HEADER]
    b = anon_connect_headers(tmp_path)[ACCOUNT_HEADER]
    assert a == b
