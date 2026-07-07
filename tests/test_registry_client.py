"""Runner fetch: registry -> cache -> offline fallback (stale flag)."""

import json

import pytest

from gecko.registry.client import RegistryFetchError, fetch_surface

MANIFEST = {
    "name": "colosseum",
    "surface_rev": "abc12345",
    "tier": "free",
    "spec": {"openapi": "3.1.0", "info": {"title": "T", "version": "1"}, "paths": {}},
}


def test_fetch_writes_cache(tmp_path):
    calls = []

    def transport(url, headers):
        calls.append((url, headers))
        return 200, json.dumps(MANIFEST)

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert got.surface_rev == "abc12345" and got.stale is False
    assert calls[0][0] == "https://registry.example.com/registry/surfaces/colosseum"
    cached = json.loads((tmp_path / "colosseum.json").read_text("utf-8"))
    assert cached["surface_rev"] == "abc12345"


def test_key_header_sent_when_given(tmp_path):
    seen = {}

    def transport(url, headers):
        seen.update(headers)
        return 200, json.dumps(MANIFEST)

    fetch_surface(
        "https://registry.example.com",
        "colosseum",
        key="gk_live_x",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert seen.get("X-Gecko-Key") == "gk_live_x"


def test_network_failure_falls_back_to_cache_stale(tmp_path):
    (tmp_path / "colosseum.json").write_text(json.dumps(MANIFEST), "utf-8")

    def transport(url, headers):
        raise OSError("network down")

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert got.stale is True and got.spec == MANIFEST["spec"]


def test_network_failure_no_cache_raises(tmp_path):
    def transport(url, headers):
        raise OSError("network down")

    with pytest.raises(RegistryFetchError):
        fetch_surface(
            "https://registry.example.com",
            "nope",
            cache_dir=tmp_path,
            transport=transport,
        )


def test_entitlement_402_raises_with_remediation(tmp_path):
    def transport(url, headers):
        return 402, json.dumps(
            {"error": "entitlement_required", "remediation": "ask for access"}
        )

    with pytest.raises(RegistryFetchError, match="entitlement_required"):
        fetch_surface(
            "https://registry.example.com",
            "txline",
            cache_dir=tmp_path,
            transport=transport,
        )
