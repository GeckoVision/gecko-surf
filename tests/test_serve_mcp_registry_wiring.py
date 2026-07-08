"""Hosted wiring: no env -> issuance disabled; never crashes the server.

Also covers the registry-store wiring in gecko.serve_mcp: colosseum is
registry-DISTRIBUTED (its console-entry runner fetches "colosseum" from the
hosted registry) but is NOT one of the MCP-hosted surfaces on this server —
distribution != hosting, and the store must carry both.
"""

import gecko.registry.wiring as wiring
import gecko.serve_mcp as serve_mcp


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


def test_registry_store_contains_colosseum_and_every_hosted_surface(monkeypatch):
    monkeypatch.delenv("REFUGIOS_APIKEY", raising=False)
    surfaces = serve_mcp._build_surfaces(hosted_enforce="block")
    hosted_names = {name for name, _ in surfaces}
    assert hosted_names == {"reportavnzla", "sosvenezuela", "txline", "jito"}

    store = serve_mcp._registry_store(surfaces)
    store_names = set(store.names())

    assert "colosseum" in store_names
    for name in hosted_names:
        assert name in store_names
    # colosseum is registry-distributed only — never becomes an MCP-hosted surface.
    assert "colosseum" not in hosted_names


def test_registry_store_includes_refugios_when_apikey_set(monkeypatch):
    monkeypatch.setenv("REFUGIOS_APIKEY", "test-key")
    surfaces = serve_mcp._build_surfaces(hosted_enforce="block")
    store = serve_mcp._registry_store(surfaces)
    assert "refugios" in store.names()
    assert "refugios" in {name for name, _ in surfaces}
