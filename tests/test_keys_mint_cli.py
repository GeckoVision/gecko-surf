"""`gecko keys mint` — the founder-run path that authorizes ONE developer directly.

The hosted email-OTP login has a live gap, so the founder mints a Gecko key from the CLI.
The invariants under test: the plaintext key is printed EXACTLY ONCE and never persisted
(the registry holds only its sha256 hash), the minted key really opens the gated surface,
and an unconfigured registry gives an actionable error instead of a silent no-op.

Offline: the in-memory registry fake is injected in place of ``registry_from_env`` — no
Mongo, no network.
"""

from __future__ import annotations

import pytest

from gecko import keyregistry
from gecko.cli import main
from gecko.keyregistry import KEY_PREFIX, InMemoryKeyRegistry, hash_key

ACCOUNT = "cofounder@example.com"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("GECKO_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MONGODB_URI", raising=False)


@pytest.fixture
def registry(monkeypatch) -> InMemoryKeyRegistry:
    store = InMemoryKeyRegistry()
    monkeypatch.setattr(keyregistry, "registry_from_env", lambda *a, **k: store)
    return store


def _minted_key(out: str) -> str:
    tokens = [w for w in out.split() if w.startswith(KEY_PREFIX)]
    assert len(tokens) == 1, f"the key must be printed exactly once, got {len(tokens)}"
    return tokens[0]


def test_mint_prints_a_gecko_key_exactly_once(registry, capsys):
    assert main(["keys", "mint", ACCOUNT]) == 0
    key = _minted_key(capsys.readouterr().out)
    assert key.startswith(KEY_PREFIX)


def test_mint_stores_only_the_hash(registry, capsys):
    main(["keys", "mint", ACCOUNT, "--label", "birdeye-cofounder"])
    key = _minted_key(capsys.readouterr().out)

    records = registry._by_hash
    assert list(records) == [hash_key(key)]
    (record,) = records.values()
    assert record["account_id"] == ACCOUNT
    assert record["enabled"] is True
    assert record["label"] == "birdeye-cofounder"
    # The plaintext key appears NOWHERE in the stored record (nor in the key of the map).
    assert key not in repr(records)


def test_minted_key_resolves_and_is_allowed(registry, capsys):
    main(["keys", "mint", ACCOUNT])
    key = _minted_key(capsys.readouterr().out)

    resolver = keyregistry.GeckoKeyResolver(registry)
    allowlist = keyregistry.RegistryAllowlist(registry)
    assert resolver(key) == ACCOUNT
    assert allowlist.is_enabled(ACCOUNT) is True
    # A random (never minted) key resolves to nothing.
    assert resolver(keyregistry.mint_key()) is None


def test_minted_key_stops_working_once_disabled(registry, capsys):
    main(["keys", "mint", ACCOUNT])
    key = _minted_key(capsys.readouterr().out)
    main(["keys", "disable", ACCOUNT])
    assert keyregistry.GeckoKeyResolver(registry)(key) is None


def test_mint_without_a_registry_is_an_actionable_error(monkeypatch, capsys):
    monkeypatch.setattr(keyregistry, "registry_from_env", lambda *a, **k: None)
    assert main(["keys", "mint", ACCOUNT]) == 2
    err = capsys.readouterr().err
    assert "MONGODB_URI" in err


def test_mint_rejects_a_blank_account(registry, capsys):
    assert main(["keys", "mint", "   "]) == 2
    assert registry._by_hash == {}


def test_mint_appears_in_help(capsys):
    main([])
    assert "keys mint" in capsys.readouterr().out


def test_cli_minted_key_opens_the_gated_surface_end_to_end(registry, capsys):
    """The whole seam offline: `keys mint` -> the served gated mount lets that key in,
    and only that key (the proof the founder command actually authorizes a developer)."""
    pytest.importorskip("mcp")
    from pathlib import Path

    from starlette.testclient import TestClient

    from gecko.http_server import build_multi_surface_app

    # --surface is what actually authorizes: a bare mint yields a key that opens nothing.
    main(["keys", "mint", ACCOUNT, "--surface", "birdeye"])
    key = _minted_key(capsys.readouterr().out)

    spec = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")
    app = build_multi_surface_app(
        [("birdeye", spec), ("jupiter", spec)],
        allowed_hosts=["testserver"],
        require_gecko_key=True,
        gated_surfaces=frozenset({"birdeye"}),
        key_registry=registry,
    )
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        allowed = client.post(
            "/birdeye/mcp",
            json=init,
            headers={**headers, "Authorization": f"Bearer {key}"},
        )
        denied = client.post("/birdeye/mcp", json=init, headers=headers)
        public = client.post("/jupiter/mcp", json=init, headers=headers)
    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert public.status_code == 200  # the funnel is untouched
