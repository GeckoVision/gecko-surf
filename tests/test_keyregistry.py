"""Gecko key registry + resolver — fully offline (in-memory fake, no Mongo, no network).

Falsifies the whole control-plane credential: mint → hash → store → resolve round-trip; the
fail-closed matrix (enabled → account, disabled → None, unknown/malformed → None); the
enable/disable toggle; and the hard invariant that the plaintext key never lands in the store,
a repr, or an error.
"""

from __future__ import annotations

import logging

import pytest

from gecko.keyregistry import (
    KEY_PREFIX,
    GeckoKeyResolver,
    InMemoryKeyRegistry,
    KeyRegistryError,
    RegistryAllowlist,
    hash_key,
    mint_key,
    registry_from_env,
)

ACCOUNT = "did:privy:dev-0001"
LABEL = "gecko login"


def _mint_and_store(registry: InMemoryKeyRegistry, account: str = ACCOUNT) -> str:
    key = mint_key()
    registry.store_key(key_hash=hash_key(key), account_id=account, label=LABEL)
    return key


# --- minting + hashing --------------------------------------------------------


def test_mint_key_shape_and_entropy():
    key = mint_key()
    assert key.startswith(KEY_PREFIX)
    body = key[len(KEY_PREFIX) :]
    assert len(body) == 43  # 256-bit base62
    assert body.isalnum()
    # Two mints never collide (CSPRNG).
    assert mint_key() != mint_key()


def test_hash_key_is_deterministic_sha256_hex():
    key = "gecko_sk_" + "a" * 43
    h = hash_key(key)
    assert h == hash_key(key)  # deterministic
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert key not in h  # the digest never contains the plaintext


# --- round trip + fail-closed matrix -----------------------------------------


def test_mint_store_resolve_round_trip():
    registry = InMemoryKeyRegistry()
    key = _mint_and_store(registry)
    resolver = GeckoKeyResolver(registry)
    assert resolver(key) == ACCOUNT


def test_disabled_key_resolves_to_none():
    registry = InMemoryKeyRegistry()
    key = _mint_and_store(registry)
    assert registry.set_account_enabled(ACCOUNT, False) == 1
    assert GeckoKeyResolver(registry)(key) is None


def test_re_enable_restores_resolution():
    registry = InMemoryKeyRegistry()
    key = _mint_and_store(registry)
    registry.set_account_enabled(ACCOUNT, False)
    registry.set_account_enabled(ACCOUNT, True)
    assert GeckoKeyResolver(registry)(key) == ACCOUNT


@pytest.mark.parametrize(
    "token",
    [
        "",
        "   ",
        "gecko_sk_never-minted-this-one",  # right shape, no record
        "sk_wrong_prefix_entirely",  # not a gecko key -> not ours
        "eyJ.a.privy.jwt",  # a Privy JWT is not a registry key
    ],
)
def test_unknown_or_malformed_resolves_to_none(token):
    registry = InMemoryKeyRegistry()
    _mint_and_store(registry)  # a valid, unrelated key exists
    assert GeckoKeyResolver(registry)(token) is None


def test_resolver_swallows_registry_error_to_deny():
    class _Boom:
        def account_for(self, key_hash):
            raise KeyRegistryError("store down")

    # Fail-closed: a store error is a deny (None), never a raise that could carry the key.
    assert GeckoKeyResolver(_Boom())(mint_key()) is None  # type: ignore[arg-type]


# --- registry allowlist (the hosted-plane enable/disable) --------------------


def test_registry_allowlist_reflects_enabled_state():
    registry = InMemoryKeyRegistry()
    _mint_and_store(registry)
    allow = RegistryAllowlist(registry)
    assert allow.is_enabled(ACCOUNT) is True
    assert allow.accounts() == [ACCOUNT]

    assert allow.disable(ACCOUNT) is True
    assert allow.is_enabled(ACCOUNT) is False
    assert allow.accounts() == []
    assert allow.disable(ACCOUNT) is False  # idempotent

    assert allow.enable(ACCOUNT) is True
    assert allow.is_enabled(ACCOUNT) is True


def test_registry_allowlist_unknown_account_is_not_enabled():
    allow = RegistryAllowlist(InMemoryKeyRegistry())
    assert allow.is_enabled("nobody") is False
    assert allow.is_enabled("") is False


def test_registry_allowlist_swallows_registry_error_to_deny():
    """R3: a raising registry made ``is_enabled`` propagate, so a Mongo blip answered
    HTTP 500 instead of the gate's clean 403 — asymmetric with ``GeckoKeyResolver``,
    which already swallows to a deny. Still fail-closed, now on the same shape."""

    class _Boom:
        def enabled_accounts(self):
            raise KeyRegistryError("store down")

    allow = RegistryAllowlist(_Boom())  # type: ignore[arg-type]
    assert allow.is_enabled(ACCOUNT) is False


def test_registry_allowlist_denial_on_store_error_never_names_the_account(caplog):
    class _Boom:
        def enabled_accounts(self):
            raise KeyRegistryError("store down")

    with caplog.at_level(logging.WARNING, logger="gecko.keyregistry"):
        RegistryAllowlist(_Boom()).is_enabled(ACCOUNT)  # type: ignore[arg-type]
    assert ACCOUNT not in " ".join(r.getMessage() for r in caplog.records)


def test_registry_allowlist_rejects_blank_account():
    allow = RegistryAllowlist(InMemoryKeyRegistry())
    with pytest.raises(KeyRegistryError):
        allow.enable("   ")


# --- the control-plane invariant: no plaintext key at rest -------------------


def test_plaintext_key_never_stored_or_reprd():
    registry = InMemoryKeyRegistry()
    key = _mint_and_store(registry)
    # The store holds the hash, the account, created, enabled, label — never the key.
    stored = registry._by_hash
    assert hash_key(key) in stored
    assert key not in repr(stored)
    for record in stored.values():
        assert key not in repr(record)
        assert set(record) == {"account_id", "created", "enabled", "label", "surfaces"}
    # The registry's own repr never surfaces the key (or the hash store).
    assert key not in repr(registry)


# --- env wiring ---------------------------------------------------------------


def test_registry_from_env_none_when_unset_or_sentinel():
    assert registry_from_env({}) is None
    assert registry_from_env({"MONGODB_URI": ""}) is None
    assert registry_from_env({"MONGODB_URI": "__unset__"}) is None
