"""Per-surface grants — "who may reach WHICH paid API", the founder-controlled half.

The finding these guard: `is_enabled` alone answered "is this account live", so any
enabled key opened EVERY gated surface, and `gecko login` minted enabled keys — making
every paid surface reachable by anyone who could pass an email OTP.

Two independent switches now: `enable/disable` (the account is live at all) and
`grant/revoke` (it may reach THIS surface). Both must say yes.
"""

from __future__ import annotations

import json

import pytest

from gecko import keyauth
from gecko.keyauth import FileAllowlist, KeyAuthError, SurfaceScopedAllowlist
from gecko.keyregistry import InMemoryKeyRegistry, RegistryAllowlist, hash_key, mint_key

ACCOUNT = "did:privy:dev-a"
PAID = "birdeye"


class _EnabledOnly:
    """A store that knows enablement but NOT grants — the pre-change contract."""

    def is_enabled(self, account: str) -> bool:
        return account == ACCOUNT


class _Grants(_EnabledOnly):
    def __init__(self, surfaces: set[str]) -> None:
        self._surfaces = surfaces

    def may_access(self, account: str, surface: str) -> bool:
        return account == ACCOUNT and surface in self._surfaces


# --- SurfaceScopedAllowlist ------------------------------------------------------


def test_enabled_and_granted_is_allowed() -> None:
    scoped = SurfaceScopedAllowlist(_Grants({PAID}), PAID)
    assert scoped.is_enabled(ACCOUNT) is True


def test_enabled_but_not_granted_is_denied() -> None:
    scoped = SurfaceScopedAllowlist(_Grants({"other-paid-api"}), PAID)
    assert scoped.is_enabled(ACCOUNT) is False


def test_granted_but_not_enabled_is_denied() -> None:
    """`disable` stays the single kill switch — it must beat any surviving grant."""
    scoped = SurfaceScopedAllowlist(_Grants({PAID}), PAID)
    assert scoped.is_enabled("someone-else") is False


def test_a_store_without_grant_support_denies_rather_than_degrades() -> None:
    """Fail-closed: swapping in a store that cannot express grants must LOCK the paid
    doors, never fall back to a bare enabled check and open them."""
    scoped = SurfaceScopedAllowlist(_EnabledOnly(), PAID)
    assert scoped.is_enabled(ACCOUNT) is False


def test_scope_gate_narrows_an_existing_gate() -> None:
    gate = keyauth.KeyGate(
        resolve_account=lambda _t: ACCOUNT, allowlist=_Grants({PAID})
    )
    assert keyauth.scope_gate(gate, PAID).decide("tok").allowed is True
    assert keyauth.scope_gate(gate, "another").decide("tok").allowed is False


# --- FileAllowlist (local / dev) -------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return FileAllowlist(path=tmp_path / "gecko-keys.json")


def test_grant_revoke_round_trip_is_idempotent(store: FileAllowlist) -> None:
    assert store.grant(ACCOUNT, PAID) is True
    assert store.grant(ACCOUNT, PAID) is False  # already held
    assert store.grants_for(ACCOUNT) == [PAID]
    assert store.may_access(ACCOUNT, PAID) is True
    assert store.revoke(ACCOUNT, PAID) is True
    assert store.revoke(ACCOUNT, PAID) is False
    assert store.may_access(ACCOUNT, PAID) is False


def test_an_ungranted_account_reaches_nothing(store: FileAllowlist) -> None:
    store.enable(ACCOUNT)
    assert store.is_enabled(ACCOUNT) is True
    assert store.may_access(ACCOUNT, PAID) is False  # enabled != allowed here


def test_enable_and_grant_do_not_clobber_each_other(store: FileAllowlist) -> None:
    """Both live in one file, so a read-modify-write bug would silently drop access."""
    store.grant(ACCOUNT, PAID)
    store.enable(ACCOUNT)
    store.grant(ACCOUNT, "second-api")
    on_disk = json.loads(store._file().read_text(encoding="utf-8"))
    assert on_disk["accounts"] == [ACCOUNT]
    assert on_disk["grants"] == {ACCOUNT: sorted([PAID, "second-api"])}


def test_a_grant_must_name_a_mount_not_a_path(store: FileAllowlist) -> None:
    for bad in ["../admin", "a/b", "https://evil.test", ""]:
        with pytest.raises(KeyAuthError, match="invalid surface name"):
            store.grant(ACCOUNT, bad)


def test_the_file_never_holds_a_token(store: FileAllowlist) -> None:
    key = mint_key()
    store.enable(ACCOUNT)
    store.grant(ACCOUNT, PAID)
    assert key not in store._file().read_text(encoding="utf-8")


# --- RegistryAllowlist (hosted) --------------------------------------------------


def _registry() -> tuple[InMemoryKeyRegistry, RegistryAllowlist]:
    registry = InMemoryKeyRegistry()
    registry.store_key(key_hash=hash_key(mint_key()), account_id=ACCOUNT, label="t")
    return registry, RegistryAllowlist(registry)


def test_registry_grants_round_trip() -> None:
    _registry_obj, allow = _registry()
    assert allow.may_access(ACCOUNT, PAID) is False  # default-deny
    assert allow.grant(ACCOUNT, PAID) is True
    assert allow.may_access(ACCOUNT, PAID) is True
    assert allow.grants_for(ACCOUNT) == [PAID]
    assert allow.revoke(ACCOUNT, PAID) is True
    assert allow.may_access(ACCOUNT, PAID) is False


def test_a_disabled_account_loses_every_grant() -> None:
    """`disable` is the kill switch: revoking access must not require un-granting."""
    registry, allow = _registry()
    allow.grant(ACCOUNT, PAID)
    registry.set_account_enabled(ACCOUNT, False)
    assert allow.may_access(ACCOUNT, PAID) is False


def test_a_registry_error_fails_closed() -> None:
    class _Broken(InMemoryKeyRegistry):
        def surfaces_for_account(self, account_id: str) -> list[str]:
            raise RuntimeError("store unreachable")

    assert RegistryAllowlist(_Broken()).may_access(ACCOUNT, PAID) is False
