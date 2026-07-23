"""Phase 1 of the local credential resolver — the $0 offline falsifier.

Proves resolution + precedence, graceful degradation, and (the security
deliverable) that a sentinel secret never leaks into any error text — with an
injected in-memory fake backend, no network and no real secret.
"""

from __future__ import annotations

import sys

import pytest

from gecko.credentials import (
    ChainResolver,
    CredentialError,
    CredentialRef,
    EnvBackend,
    KeyringBackend,
    default_resolver,
    env_visible_names,
    keyring_fallback_banner,
    no_credential_message,
    ref_from_slot,
    which_backend,
)

SENTINEL = "SENTINEL-DO-NOT-LEAK"


class _FakeKeyring:
    """Light fake of the ``keyring`` module — in-memory, no OS store, no network.

    Enough of the surface KeyringBackend touches: ``get_keyring`` (for the
    availability probe) plus get/set/delete_password. Injected via
    ``KeyringBackend(module=...)`` or ``sys.modules['keyring']``.
    """

    def __init__(self, backend_obj: object | None = None) -> None:
        self._store: dict[tuple[str, str], str] = {}
        self._backend = backend_obj if backend_obj is not None else object()

    def get_keyring(self) -> object:
        return self._backend

    def set_password(self, service: str, user: str, password: str) -> None:
        self._store[(service, user)] = password

    def get_password(self, service: str, user: str) -> str | None:
        return self._store.get((service, user))

    def delete_password(self, service: str, user: str) -> None:
        del self._store[(service, user)]


def _null_backend() -> object:
    """A backend object that looks like keyring's fail/null backend (no real store)."""
    cls = type("NullKeyring", (), {})
    cls.__module__ = "keyring.backends.null"
    return cls()


class FakeBackend:
    """Light in-memory backend — deterministic, no network (per the spec sketch)."""

    def __init__(self, name: str, store: dict[str, str], up: bool = True) -> None:
        self.name = name
        self._store = store
        self._up = up

    def available(self) -> bool:
        return self._up

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


# --- CredentialRef -----------------------------------------------------------


def test_slot_without_account() -> None:
    assert CredentialRef(api="txodds").slot() == "txodds"


def test_slot_with_account() -> None:
    assert CredentialRef(api="colosseum", account="alt").slot() == "colosseum:alt"


# --- 1. Resolution + precedence ---------------------------------------------


def test_first_hit_wins_higher_precedence_beats_lower() -> None:
    high = FakeBackend("high", {"txodds": "HIGH"})
    low = FakeBackend("low", {"txodds": "LOW"})
    resolver = ChainResolver([high, low])
    assert resolver.resolve(CredentialRef(api="txodds")) == "HIGH"


def test_miss_falls_through_to_next_backend() -> None:
    high = FakeBackend("high", {})  # miss
    low = FakeBackend("low", {"txodds": "LOW"})
    resolver = ChainResolver([high, low])
    assert resolver.resolve(CredentialRef(api="txodds")) == "LOW"


def test_all_miss_raises_with_slot_and_remediation() -> None:
    resolver = ChainResolver([FakeBackend("a", {}), FakeBackend("b", {})])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    msg = str(exc.value)
    assert "txodds" in msg  # the ref slot
    assert "a" in msg and "b" in msg  # backend names tried
    assert "gecko auth set" in msg  # remediation hint


def test_account_scoped_slot_resolves_independently() -> None:
    store = {"colosseum": "DEFAULT", "colosseum:alt": "ALT"}
    resolver = ChainResolver([FakeBackend("fake", store)])
    assert resolver.resolve(CredentialRef("colosseum")) == "DEFAULT"
    assert resolver.resolve(CredentialRef("colosseum", "alt")) == "ALT"


# --- 2. Degradation ----------------------------------------------------------


def test_unavailable_backend_is_skipped_no_crash() -> None:
    down = FakeBackend("down", {"txodds": "SHOULD-NOT-BE-READ"}, up=False)
    up = FakeBackend("up", {"txodds": "FROM-UP"})
    resolver = ChainResolver([down, up])
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-UP"


def test_unavailable_backend_not_listed_in_tried() -> None:
    down = FakeBackend("keyring", {}, up=False)
    resolver = ChainResolver([down])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    # a skipped backend was never tried -> not named as attempted
    assert "keyring" not in str(exc.value)


def test_all_unavailable_reports_none_tried() -> None:
    resolver = ChainResolver([FakeBackend("a", {}, up=False)])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    assert "none" in str(exc.value)


# --- 3. Redaction / leak (the security falsifier) ---------------------------


def test_sentinel_never_leaks_on_all_miss() -> None:
    # A live secret exists in a backend that is DOWN; resolution must miss and
    # the raised error must not carry the value that a down backend held.
    down = FakeBackend("keyring", {"txodds": SENTINEL}, up=False)
    resolver = ChainResolver([down])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    assert SENTINEL not in str(exc.value)
    assert SENTINEL not in repr(exc.value)


def test_error_contains_only_ref_backend_remediation() -> None:
    resolver = ChainResolver([FakeBackend("env", {})])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    msg = str(exc.value)
    assert "txodds" in msg
    assert "env" in msg
    assert "gecko auth set" in msg


def test_env_backend_error_path_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with the sentinel present under a DIFFERENT api, resolving a missing
    # one must not surface any value.
    monkeypatch.setenv("GECKO_CRED_OTHER", SENTINEL)
    resolver = ChainResolver([EnvBackend()])
    with pytest.raises(CredentialError) as exc:
        resolver.resolve(CredentialRef(api="txodds"))
    assert SENTINEL not in str(exc.value)


# --- 4. EnvBackend -----------------------------------------------------------


def test_env_backend_resolves_gecko_cred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_CRED_TXODDS", "env-token")
    backend = EnvBackend()
    assert backend.get(CredentialRef(api="txodds")) == "env-token"


def test_env_backend_account_slot_uppercased(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_CRED_COLOSSEUM_ALT", "scoped")
    backend = EnvBackend()
    assert backend.get(CredentialRef(api="colosseum", account="alt")) == "scoped"


def test_env_backend_resolves_configured_legacy_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GECKO_CRED_TXODDS", raising=False)
    monkeypatch.setenv("TXODDS_API_TOKEN", "legacy-token")
    backend = EnvBackend(legacy_names={"txodds": "TXODDS_API_TOKEN"})
    assert backend.get(CredentialRef(api="txodds")) == "legacy-token"


def test_env_backend_canonical_beats_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_CRED_TXODDS", "canonical")
    monkeypatch.setenv("TXODDS_API_TOKEN", "legacy")
    backend = EnvBackend(legacy_names={"txodds": "TXODDS_API_TOKEN"})
    assert backend.get(CredentialRef(api="txodds")) == "canonical"


def test_env_backend_unset_is_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_TXODDS", raising=False)
    assert EnvBackend().get(CredentialRef(api="txodds")) is None


def test_env_backend_empty_is_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GECKO_CRED_TXODDS", "")
    assert EnvBackend().get(CredentialRef(api="txodds")) is None


def test_env_backend_empty_legacy_is_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_TXODDS", raising=False)
    monkeypatch.setenv("TXODDS_API_TOKEN", "")
    backend = EnvBackend(legacy_names={"txodds": "TXODDS_API_TOKEN"})
    assert backend.get(CredentialRef(api="txodds")) is None


def test_env_backend_always_available() -> None:
    assert EnvBackend().available() is True


def test_env_backend_colosseum_legacy_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_COLOSSEUM", raising=False)
    monkeypatch.setenv("COLOSSEUM_COPILOT_PAT", "pat-value")
    backend = EnvBackend(legacy_names={"colosseum": "COLOSSEUM_COPILOT_PAT"})
    assert backend.get(CredentialRef(api="colosseum")) == "pat-value"


# --- Protocol conformance ----------------------------------------------------


def test_backends_satisfy_protocol() -> None:
    from gecko.credentials import CredentialBackend

    assert isinstance(EnvBackend(), CredentialBackend)
    assert isinstance(FakeBackend("fake", {}), CredentialBackend)
    assert isinstance(KeyringBackend(), CredentialBackend)


# --- 5. KeyringBackend (injected fake — no real OS keychain) -----------------


def test_keyring_store_get_roundtrip() -> None:
    backend = KeyringBackend(module=_FakeKeyring())
    ref = CredentialRef(api="txodds")
    assert backend.available() is True
    assert backend.get(ref) is None  # miss before store
    backend.store(ref, "SECRET-TOKEN")
    assert backend.get(ref) == "SECRET-TOKEN"


def test_keyring_unavailable_when_import_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `keyring` is now a BASE dependency (always importable), so absence is simulated by
    # forcing `import keyring` to raise ImportError (sys.modules[...] = None). The degrade
    # path — available() -> False when the import misses — must still hold.
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert KeyringBackend().available() is False


def test_keyring_unavailable_when_null_backend() -> None:
    backend = KeyringBackend(module=_FakeKeyring(backend_obj=_null_backend()))
    assert backend.available() is False


def test_keyring_get_miss_returns_none() -> None:
    backend = KeyringBackend(module=_FakeKeyring())
    assert backend.get(CredentialRef(api="absent")) is None


def test_keyring_store_requires_available_backend() -> None:
    backend = KeyringBackend(module=_FakeKeyring(backend_obj=_null_backend()))
    with pytest.raises(CredentialError):
        backend.store(CredentialRef(api="txodds"), "SECRET")


def test_keyring_store_error_never_leaks_secret() -> None:
    backend = KeyringBackend(module=_FakeKeyring(backend_obj=_null_backend()))
    with pytest.raises(CredentialError) as exc:
        backend.store(CredentialRef(api="txodds"), SENTINEL)
    assert SENTINEL not in str(exc.value)


def test_keyring_delete_idempotent_and_reports() -> None:
    backend = KeyringBackend(module=_FakeKeyring())
    ref = CredentialRef(api="txodds")
    assert backend.delete(ref) is False  # nothing there yet
    backend.store(ref, "S")
    assert backend.delete(ref) is True  # existed
    assert backend.delete(ref) is False  # idempotent second delete


def test_keyring_list_slots_tracks_index() -> None:
    backend = KeyringBackend(module=_FakeKeyring())
    backend.store(CredentialRef(api="txodds"), "A")
    backend.store(CredentialRef(api="colosseum", account="alt"), "B")
    assert backend.list_slots() == ["colosseum:alt", "txodds"]
    backend.delete(CredentialRef(api="txodds"))
    assert backend.list_slots() == ["colosseum:alt"]


def test_keyring_index_holds_names_not_values() -> None:
    fake = _FakeKeyring()
    backend = KeyringBackend(module=fake)
    backend.store(CredentialRef(api="txodds"), SENTINEL)
    index_raw = fake.get_password("gecko:__index__", "gecko") or ""
    assert "txodds" in index_raw  # the name is indexed
    assert SENTINEL not in index_raw  # the value is NOT


# --- 6. default_resolver: precedence + GECKO_CRED_BACKEND pin -----------------


def _inject_keyring(
    monkeypatch: pytest.MonkeyPatch, slot: str, value: str
) -> _FakeKeyring:
    fake = _FakeKeyring()
    fake.set_password(f"gecko:{slot}", "gecko", value)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def test_default_resolver_keyring_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    _inject_keyring(monkeypatch, "txodds", "FROM-KEYRING")
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")
    resolver = default_resolver()
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-KEYRING"


def test_default_resolver_falls_through_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)  # keyring absent
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")
    resolver = default_resolver()
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-ENV"


def test_backend_pin_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _inject_keyring(monkeypatch, "txodds", "FROM-KEYRING")
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")
    monkeypatch.setenv("GECKO_CRED_BACKEND", "env")
    resolver = default_resolver()
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-ENV"


def test_backend_pin_keyring_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _inject_keyring(monkeypatch, "txodds", "FROM-KEYRING")
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")
    monkeypatch.setenv("GECKO_CRED_BACKEND", "keyring")
    resolver = default_resolver()
    assert resolver.resolve(CredentialRef(api="txodds")) == "FROM-KEYRING"


def test_backend_pin_command_is_deterministic_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Phase 3 command backend is unbuilt: pinning it must MISS deterministically,
    # never silently read the developer keychain or an ambient env var.
    _inject_keyring(monkeypatch, "txodds", "FROM-KEYRING")
    monkeypatch.setenv("GECKO_CRED_TXODDS", "FROM-ENV")
    monkeypatch.setenv("GECKO_CRED_BACKEND", "command")
    resolver = default_resolver()
    with pytest.raises(CredentialError):
        resolver.resolve(CredentialRef(api="txodds"))


# --- 7. Degradation banner + audit helpers -----------------------------------


def test_banner_when_keyring_down_env_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.setitem(
        sys.modules, "keyring", None
    )  # force ImportError (keyring absent)
    monkeypatch.setenv("GECKO_CRED_TXODDS", "x")
    resolver = default_resolver()
    msg = keyring_fallback_banner(CredentialRef(api="txodds"), resolver)
    assert msg is not None
    assert "keyring unavailable" in msg
    assert "GECKO_CRED_TXODDS" in msg


def test_no_banner_when_keyring_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    _inject_keyring(monkeypatch, "txodds", "kr")
    monkeypatch.setenv("GECKO_CRED_TXODDS", "x")
    resolver = default_resolver()
    assert keyring_fallback_banner(CredentialRef(api="txodds"), resolver) is None


def test_no_banner_when_nothing_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.delenv("GECKO_CRED_TXODDS", raising=False)
    resolver = default_resolver()
    assert keyring_fallback_banner(CredentialRef(api="txodds"), resolver) is None


def test_banner_never_leaks_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.setenv("GECKO_CRED_TXODDS", SENTINEL)
    resolver = default_resolver()
    msg = keyring_fallback_banner(CredentialRef(api="txodds"), resolver) or ""
    assert SENTINEL not in msg


def test_no_credential_message_is_actionable() -> None:
    msg = no_credential_message(CredentialRef(api="colosseum"))
    assert "gecko auth set colosseum" in msg
    assert "GECKO_CRED_COLOSSEUM" in msg


def test_ref_from_slot_roundtrips() -> None:
    assert ref_from_slot("txodds") == CredentialRef(api="txodds")
    assert ref_from_slot("colosseum:alt") == CredentialRef("colosseum", "alt")


def test_which_backend_names_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.setenv("GECKO_CRED_TXODDS", "x")
    resolver = default_resolver()
    assert which_backend(CredentialRef(api="txodds"), resolver) == "env"


def test_which_backend_none_when_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GECKO_CRED_BACKEND", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.delenv("GECKO_CRED_TXODDS", raising=False)
    resolver = default_resolver()
    assert which_backend(CredentialRef(api="txodds"), resolver) is None


def test_env_visible_names_excludes_backend_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GECKO_CRED_TXODDS", "x")
    monkeypatch.setenv("GECKO_CRED_BACKEND", "env")
    names = env_visible_names()
    assert "GECKO_CRED_TXODDS" in names
    assert "GECKO_CRED_BACKEND" not in names


def test_a_backend_that_raises_on_read_falls_through_to_the_next() -> None:
    """The connect-blocking bug: a present-but-broken keychain (macOS -25244) whose
    read RAISES must be treated as a MISS so the chain falls through to the env var —
    not crash `gecko connect` before it can use GECKO_CRED_GECKO_IDENTITY."""
    import os as _os

    from gecko.credentials import ChainResolver, CredentialRef, EnvBackend

    ref = CredentialRef(api="gecko-identity")

    class _BrokenBackend:
        name = "keyring"

        def available(self) -> bool:
            return True

        def get(self, ref):
            raise RuntimeError("(-25244) keychain interaction not allowed")

    _os.environ["GECKO_CRED_GECKO_IDENTITY"] = "gecko_sk_fromenv"
    try:
        chain = ChainResolver(backends=[_BrokenBackend(), EnvBackend()])
        assert chain.resolve(ref) == "gecko_sk_fromenv"  # fell through, no crash
    finally:
        _os.environ.pop("GECKO_CRED_GECKO_IDENTITY", None)
