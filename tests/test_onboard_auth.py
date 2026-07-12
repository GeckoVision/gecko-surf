"""Tests for auth detection and key storage in onboarding."""

from gecko.onboard import ensure_key, spec_needs_auth


def test_detects_declared_auth():
    assert spec_needs_auth(
        {"components": {"securitySchemes": {"k": {"type": "apiKey"}}}}
    )
    assert not spec_needs_auth({"paths": {}})


def test_ensure_key_stores_when_prompted():
    stored = {}

    def _store(name: str, secret: str) -> bool:
        stored[name] = secret
        return True

    ok = ensure_key("stripe", prompt=lambda q: "sk-live-x", store=_store)
    assert ok and stored == {"stripe": "sk-live-x"}


def test_ensure_key_skips_on_empty():
    ok = ensure_key("stripe", prompt=lambda q: "", store=lambda n, s: True)
    assert not ok


def test_ensure_key_reports_failure_when_store_does_not_persist():
    """A degraded/unavailable keychain: non-empty secret, store() returns False."""
    ok = ensure_key("stripe", prompt=lambda q: "sk-live-x", store=lambda n, s: False)
    assert not ok
