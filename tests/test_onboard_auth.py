"""Tests for auth detection and key storage in onboarding."""

from gecko.onboard import ensure_key, spec_needs_auth


def test_detects_declared_auth():
    assert spec_needs_auth(
        {"components": {"securitySchemes": {"k": {"type": "apiKey"}}}}
    )
    assert not spec_needs_auth({"paths": {}})


def test_ensure_key_stores_when_prompted():
    stored = {}
    ok = ensure_key(
        "stripe",
        prompt=lambda q: "sk-live-x",
        store=lambda name, secret: stored.__setitem__(name, secret),
    )
    assert ok and stored == {"stripe": "sk-live-x"}


def test_ensure_key_skips_on_empty():
    ok = ensure_key("stripe", prompt=lambda q: "", store=lambda n, s: None)
    assert not ok
