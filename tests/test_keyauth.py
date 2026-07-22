"""Layer 1 access control: verify a Gecko key + a founder allowlist, default-deny.

Pure/offline — a fake resolver stands in for the server-side token verifier, and the
allowlist is either an in-memory fake or the real 0600 file store. The recurring
assertion across every path: the token value NEVER appears in a decision, an error,
a repr, or the log.
"""

from __future__ import annotations

import json
import logging

import pytest

from gecko.keyauth import (
    AuthDecision,
    FileAllowlist,
    KeyAuthError,
    KeyGate,
    authorize,
    deny_all_resolver,
)

# A secret-shaped Gecko key; every test asserts it never leaks.
TOKEN = "eyJ-SECRET-gecko-key-DO-NOT-LEAK.aaa.bbb"
ACCOUNT = "did:privy:enabled-dev"


class _SetAllowlist:
    """A light in-memory allowlist fake (no file, no keychain)."""

    def __init__(self, enabled: set[str]) -> None:
        self._enabled = enabled

    def is_enabled(self, account: str) -> bool:
        return account in self._enabled


def _resolver(mapping: dict[str, str]):
    """A fake token->account resolver: known token maps to an account, else invalid."""

    def resolve(token: str) -> str | None:
        return mapping.get(token)

    return resolve


# --- authorize: the four decisions -------------------------------------------


def test_valid_and_enabled_allows():
    decision = authorize(
        TOKEN,
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist({ACCOUNT}),
    )
    assert decision == AuthDecision(allowed=True, account=ACCOUNT, reason="ok")


def test_valid_but_not_enabled_denies_and_names_the_account():
    decision = authorize(
        TOKEN,
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist(set()),  # resolves, but nobody enabled
    )
    assert not decision.allowed
    assert decision.reason == "not_enabled"
    assert decision.account == ACCOUNT  # so the founder can see who to enable


def test_invalid_token_denies():
    decision = authorize(
        "not-a-real-key",
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist({ACCOUNT}),
    )
    assert not decision.allowed
    assert decision.reason == "invalid_token"
    assert decision.account is None


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token_denies(token):
    decision = authorize(
        token,
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist({ACCOUNT}),
    )
    assert not decision.allowed
    assert decision.reason == "missing_token"


def test_default_deny_when_allowlist_empty():
    # A valid, resolvable token still fails when NOBODY is enabled (fail-closed).
    decision = authorize(
        TOKEN,
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist(set()),
    )
    assert not decision.allowed


def test_deny_all_resolver_denies_everyone():
    decision = authorize(
        TOKEN,
        resolve_account=deny_all_resolver,
        allowlist=_SetAllowlist({ACCOUNT}),
    )
    assert not decision.allowed
    assert decision.reason == "invalid_token"


# --- the token never leaks ---------------------------------------------------


def test_token_never_appears_in_decision_or_repr():
    decision = authorize(
        TOKEN,
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist({ACCOUNT}),
    )
    assert TOKEN not in repr(decision)
    assert TOKEN not in str(decision)


def test_token_never_logged_on_any_path(caplog):
    gate = KeyGate(
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist(set()),
    )
    with caplog.at_level(logging.DEBUG):
        for tok in (TOKEN, "bad", None):
            gate.decide(tok)
    assert TOKEN not in caplog.text


# --- KeyGate is a thin bundle over authorize ---------------------------------


def test_key_gate_decide_matches_authorize():
    gate = KeyGate(
        resolve_account=_resolver({TOKEN: ACCOUNT}),
        allowlist=_SetAllowlist({ACCOUNT}),
    )
    assert gate.decide(TOKEN).allowed
    assert not gate.decide("bad").allowed


# --- FileAllowlist: the local founder store ----------------------------------


def test_file_allowlist_enable_then_authorize_allows(tmp_path):
    store = FileAllowlist(path=tmp_path / "gecko-keys.json")
    assert store.enable(ACCOUNT) is True
    assert store.enable(ACCOUNT) is False  # idempotent
    decision = authorize(
        TOKEN, resolve_account=_resolver({TOKEN: ACCOUNT}), allowlist=store
    )
    assert decision.allowed


def test_file_allowlist_disable_then_authorize_denies(tmp_path):
    store = FileAllowlist(path=tmp_path / "gecko-keys.json")
    store.enable(ACCOUNT)
    assert store.disable(ACCOUNT) is True
    assert store.disable(ACCOUNT) is False  # idempotent
    decision = authorize(
        TOKEN, resolve_account=_resolver({TOKEN: ACCOUNT}), allowlist=store
    )
    assert not decision.allowed
    assert decision.reason == "not_enabled"


def test_file_allowlist_lists_accounts_never_tokens(tmp_path):
    path = tmp_path / "gecko-keys.json"
    store = FileAllowlist(path=path)
    store.enable("dev-b")
    store.enable("dev-a")
    assert store.accounts() == ["dev-a", "dev-b"]  # sorted, ids only
    # The token never reaches the file by construction — the store only sees accounts.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"accounts": ["dev-a", "dev-b"], "grants": {}}
    assert TOKEN not in path.read_text(encoding="utf-8")


def test_file_allowlist_file_is_owner_only(tmp_path):
    path = tmp_path / "gecko-keys.json"
    FileAllowlist(path=path).enable(ACCOUNT)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_file_allowlist_missing_file_is_empty_not_error(tmp_path):
    store = FileAllowlist(path=tmp_path / "does-not-exist.json")
    assert store.accounts() == []
    assert store.is_enabled(ACCOUNT) is False


def test_file_allowlist_rejects_blank_account(tmp_path):
    store = FileAllowlist(path=tmp_path / "gecko-keys.json")
    with pytest.raises(KeyAuthError):
        store.enable("   ")


def test_default_path_follows_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))
    store = FileAllowlist()  # no explicit path -> config home
    store.enable(ACCOUNT)
    assert (tmp_path / "gecko-keys.json").exists()
